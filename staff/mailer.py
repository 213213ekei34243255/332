"""Transactional email through Google SMTP (Gmail / Google Workspace).

Gmail requires an *App Password* (Google Account -> Security -> 2-Step
Verification -> App passwords) used with SMTP_USER; port 465 = implicit TLS,
port 587 = STARTTLS. Mail is queued in the database and sent by a background
worker with retries, so a slow SMTP handshake never blocks a request and a
failure is visible (and resendable) from the /staff dashboard.
"""
import logging
import queue
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape

from .config import get_settings
from .db import now_ms, new_id

log = logging.getLogger("staff.mailer")
_HEADER_BAD = re.compile(r"[\r\n]+")


class EmailError(Exception):
    pass


def _safe_header(text):
    return _HEADER_BAD.sub(" ", str(text or "")).strip()


def _friendly_smtp_error(exc):
    """Turn raw smtplib failures into something an admin can act on (no secrets)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("Google rejected the SMTP login. Use a 16-character App Password "
                "(2-Step Verification must be on) in SMTP_PASSWORD and the full Gmail address in SMTP_USER.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The recipient address was refused by the mail server."
    if isinstance(exc, (smtplib.SMTPConnectError, ConnectionError, TimeoutError, OSError)):
        return ("Could not reach the SMTP server. Render free web services block outbound SMTP "
                "(ports 25/465/587); a paid instance is required for Google SMTP.")
    if isinstance(exc, smtplib.SMTPException):
        return f"SMTP error: {str(exc)[:160]}"
    return f"Email failed: {type(exc).__name__}"


def build_message(to_addr, subject, text_body, html_body, reply_to=None):
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = _safe_header(subject)
    msg["From"] = formataddr((_safe_header(s.smtp_from_name), s.smtp_from_email or s.smtp_user))
    msg["To"] = _safe_header(to_addr)
    if reply_to or s.support_email:
        msg["Reply-To"] = _safe_header(reply_to or s.support_email)
    msg["Date"] = formatdate(localtime=False)
    domain = (s.smtp_from_email or s.smtp_user or "localhost").split("@")[-1]
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["X-Auto-Response-Suppress"] = "OOF, AutoReply"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def deliver(msg):
    """Open an SMTP connection and send one message. Raises on failure."""
    s = get_settings()
    if s.email_dry_run:
        log.info("EMAIL DRY RUN to=%s subject=%s", msg["To"], msg["Subject"])
        return
    if not s.smtp_configured:
        raise EmailError("SMTP is not configured (SMTP_USER / SMTP_PASSWORD missing).")
    context = ssl.create_default_context()
    if s.smtp_port == 465:
        server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=25)
    else:
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=25)
    try:
        server.ehlo()
        if s.smtp_port != 465:
            server.starttls(context=context)
            server.ehlo()
        server.login(s.smtp_user, s.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


class Mailer:
    """Persisted outbox + background sender."""

    RETRY_DELAYS = (4, 20, 60)

    def __init__(self, db):
        self.db = db
        self._q = queue.Queue()
        self._thread = None
        self._guard = threading.Lock()

    def _ensure_worker(self):
        with self._guard:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="staff-mailer", daemon=True)
                self._thread.start()

    def enqueue(self, *, to_addr, subject, text_body, html_body, kind, application_id=None,
                enrollment_id=None, reply_to=None):
        email_id = new_id("eml")
        self.db.insert("emails", {
            "id": email_id, "application_id": application_id, "enrollment_id": enrollment_id,
            "kind": kind, "to_addr": to_addr, "subject": subject[:300], "status": "queued",
            "attempts": 0, "error": None, "created_at": now_ms(), "sent_at": None,
        })
        self._q.put({"id": email_id, "to": to_addr, "subject": subject, "text": text_body,
                     "html": html_body, "reply_to": reply_to})
        self._ensure_worker()
        return email_id

    def send_now(self, *, to_addr, subject, text_body, html_body, kind, application_id=None,
                 enrollment_id=None):
        """Synchronous send used by the 'test email' button; returns (ok, error)."""
        email_id = new_id("eml")
        self.db.insert("emails", {
            "id": email_id, "application_id": application_id, "enrollment_id": enrollment_id,
            "kind": kind, "to_addr": to_addr, "subject": subject[:300], "status": "queued",
            "attempts": 1, "error": None, "created_at": now_ms(), "sent_at": None,
        })
        try:
            deliver(build_message(to_addr, subject, text_body, html_body))
        except Exception as exc:
            err = _friendly_smtp_error(exc) if not isinstance(exc, EmailError) else str(exc)
            self.db.update("emails", email_id, {"status": "failed", "error": err[:300]})
            return False, err
        self.db.update("emails", email_id, {"status": "sent", "sent_at": now_ms(), "error": None})
        return True, ""

    def _loop(self):
        while True:
            job = self._q.get()
            try:
                self._process(job)
            except Exception:
                log.exception("mailer job crashed")
            finally:
                self._q.task_done()

    def _process(self, job):
        last_error = ""
        for attempt in range(len(self.RETRY_DELAYS) + 1):
            self.db.run("UPDATE emails SET attempts = ? WHERE id = ?", (attempt + 1, job["id"]))
            try:
                deliver(build_message(job["to"], job["subject"], job["text"], job["html"], job.get("reply_to")))
                self.db.update("emails", job["id"], {"status": "sent", "sent_at": now_ms(), "error": None})
                return
            except smtplib.SMTPAuthenticationError as exc:  # retrying will not help
                last_error = _friendly_smtp_error(exc)
                break
            except Exception as exc:
                last_error = str(exc) if isinstance(exc, EmailError) else _friendly_smtp_error(exc)
                log.warning("email attempt %s failed: %s", attempt + 1, type(exc).__name__)
                if isinstance(exc, EmailError):
                    break
                if attempt < len(self.RETRY_DELAYS):
                    time.sleep(self.RETRY_DELAYS[attempt])
        self.db.update("emails", job["id"], {"status": "failed", "error": last_error[:300]})

    def wait_idle(self, timeout=10):
        """Test helper: block until the queue is drained."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False


# ---------------------------------------------------------------- templates
_STYLE_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _shell(title, preheader, inner_html):
    s = get_settings()
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f3f5f9;font-family:{_STYLE_FONT};color:#1b2433;">
<span style="display:none;max-height:0;overflow:hidden;opacity:0;">{escape(preheader)}</span>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f5f9;padding:28px 12px;">
<tr><td align="center">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e3e8f0;">
<tr><td style="background:#0f2a4a;padding:22px 30px;color:#ffffff;font-size:18px;font-weight:600;letter-spacing:.2px;">{escape(s.org_name)}
<div style="font-size:12px;font-weight:400;opacity:.75;margin-top:2px;">Talent Acquisition</div></td></tr>
<tr><td style="padding:30px;font-size:15px;line-height:1.6;">{inner_html}</td></tr>
<tr><td style="padding:18px 30px;background:#f8f9fc;border-top:1px solid #e3e8f0;font-size:12px;color:#6b778c;line-height:1.5;">
This message was sent by {escape(s.org_name)} because an application was submitted with this email address.
If this wasn&rsquo;t you, you can safely ignore it.</td></tr>
</table></td></tr></table></body></html>"""


def _button(url, label):
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" style="margin:22px 0;"><tr>'
            f'<td style="background:#1463d8;border-radius:9px;"><a href="{escape(url, quote=True)}" '
            f'style="display:inline-block;padding:13px 26px;color:#ffffff;text-decoration:none;font-weight:600;'
            f'font-size:15px;">{escape(label)}</a></td></tr></table>')


def render_invite(*, name, position_title, code, link, expires_text, duration_minutes, org_name,
                  support_email, custom_message=""):
    first = (name or "there").split()[0]
    subject = f"Your interview invitation for {position_title} — {org_name}"
    steps = [
        "Open the interview link above (or go to the interview page and type your code).",
        "Allow camera and microphone access when your browser asks.",
        "Complete a short device check and a quick face verification.",
        "Have a spoken conversation with our AI interviewer about the role and your experience.",
        "The session ends automatically; you will see a confirmation on screen.",
    ]
    prep = [
        "A quiet, well-lit room where you will not be interrupted",
        "A laptop or desktop with a working camera and microphone (latest Chrome, Edge or Safari)",
        "A stable internet connection and headphones if possible",
        f"About {duration_minutes} minutes of uninterrupted time",
    ]
    extra = ""
    if custom_message:
        extra = (f'<p style="margin:0 0 16px;padding:12px 14px;background:#f3f7ff;border-left:3px solid #1463d8;'
                 f'border-radius:6px;">{escape(custom_message)}</p>')
    html = f"""
<p style="margin:0 0 14px;font-size:17px;font-weight:600;">Hello {escape(first)},</p>
<p style="margin:0 0 14px;">Thank you for applying for the <strong>{escape(position_title)}</strong> position at
{escape(org_name)}. We would like to invite you to the next step: a recorded interview with our AI interviewer,
which you can take at a time that suits you.</p>
{extra}
<p style="margin:18px 0 6px;font-size:13px;color:#6b778c;text-transform:uppercase;letter-spacing:.08em;">Your enrollment code</p>
<div style="font-family:'SFMono-Regular',Consolas,'Courier New',monospace;font-size:28px;font-weight:700;letter-spacing:.14em;
background:#f3f7ff;border:1px dashed #1463d8;border-radius:10px;padding:14px 18px;text-align:center;color:#0f2a4a;">{escape(code)}</div>
{_button(link, "Start my interview")}
<p style="margin:0 0 16px;font-size:13px;color:#6b778c;">This code is personal to you, works once, and expires on
<strong>{escape(expires_text)}</strong>. Please do not share it.</p>
<p style="margin:22px 0 8px;font-weight:600;">How the interview works</p>
<ol style="margin:0 0 16px;padding-left:20px;">{''.join(f'<li style="margin-bottom:5px;">{escape(x)}</li>' for x in steps)}</ol>
<p style="margin:0 0 8px;font-weight:600;">Before you begin, please make sure you have</p>
<ul style="margin:0 0 16px;padding-left:20px;">{''.join(f'<li style="margin-bottom:5px;">{escape(x)}</li>' for x in prep)}</ul>
<p style="margin:0 0 16px;padding:12px 14px;background:#fff8ec;border-left:3px solid #e0932a;border-radius:6px;font-size:14px;">
<strong>Please note:</strong> the session is audio and video recorded, and an AI system helps summarise your answers.
Every recording and assessment is reviewed by our hiring team, and people &mdash; not software &mdash; make the final decision.
By starting the interview you consent to this recording.</p>
<p style="margin:0;">If you have any trouble, just reply to this email{(' or write to ' + escape(support_email)) if support_email else ''}.
We look forward to speaking with you.</p>
<p style="margin:18px 0 0;">Kind regards,<br><strong>The {escape(org_name)} Recruitment Team</strong></p>"""
    text = (
        f"Hello {first},\n\n"
        f"Thank you for applying for the {position_title} position at {org_name}. We would like to invite you to the "
        f"next step: a recorded interview with our AI interviewer, which you can take at a time that suits you.\n\n"
        + (f"{custom_message}\n\n" if custom_message else "")
        + f"YOUR ENROLLMENT CODE: {code}\n"
        f"Start here: {link}\n"
        f"The code is personal to you, works once, and expires on {expires_text}. Please do not share it.\n\n"
        "HOW THE INTERVIEW WORKS\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(steps, 1)) + "\n\n"
        "BEFORE YOU BEGIN, PLEASE MAKE SURE YOU HAVE\n" + "\n".join(f"- {x}" for x in prep) + "\n\n"
        "Please note: the session is audio and video recorded, and an AI system helps summarise your answers. "
        "Every recording and assessment is reviewed by our hiring team, and people make the final decision. "
        "By starting the interview you consent to this recording.\n\n"
        "If you have any trouble, just reply to this email"
        + (f" or write to {support_email}" if support_email else "") + ".\n\n"
        f"Kind regards,\nThe {org_name} Recruitment Team\n"
    )
    return subject, text, _shell(subject, f"Your interview code: {code}", html)


def render_received(*, name, position_title, org_name):
    first = (name or "there").split()[0]
    subject = f"We received your application for {position_title} — {org_name}"
    html = f"""
<p style="margin:0 0 14px;font-size:17px;font-weight:600;">Hello {escape(first)},</p>
<p style="margin:0 0 14px;">Thank you for applying for the <strong>{escape(position_title)}</strong> position at {escape(org_name)}.
We have received your application and our team will review it shortly.</p>
<p style="margin:0 0 14px;">If your profile matches what we are looking for, you will receive a separate email with a personal
interview code and instructions for the next step. You do not need to do anything else for now.</p>
<p style="margin:18px 0 0;">Kind regards,<br><strong>The {escape(org_name)} Recruitment Team</strong></p>"""
    text = (f"Hello {first},\n\nThank you for applying for the {position_title} position at {org_name}. "
            "We have received your application and our team will review it shortly.\n\n"
            "If your profile matches what we are looking for, you will receive a separate email with a personal "
            "interview code and instructions for the next step. You do not need to do anything else for now.\n\n"
            f"Kind regards,\nThe {org_name} Recruitment Team\n")
    return subject, text, _shell(subject, "Application received", html)


def render_admin_notice(*, applicant, position_title, eligibility_text, dashboard_url, org_name):
    subject = f"New application: {applicant} — {position_title}"
    html = f"""
<p style="margin:0 0 14px;font-size:16px;font-weight:600;">New application received</p>
<p style="margin:0 0 8px;"><strong>{escape(applicant)}</strong> applied for <strong>{escape(position_title)}</strong>.</p>
<p style="margin:0 0 14px;color:#6b778c;">{escape(eligibility_text)}</p>{_button(dashboard_url, "Open the staff dashboard")}"""
    text = f"{applicant} applied for {position_title}.\n{eligibility_text}\nDashboard: {dashboard_url}\n"
    return subject, text, _shell(subject, "New application", html)


def render_test(*, org_name):
    subject = f"{org_name} recruitment — SMTP test"
    html = ("<p style='margin:0 0 12px;font-size:16px;font-weight:600;'>SMTP is working.</p>"
            "<p style='margin:0;'>This is a test message from the staff dashboard. Interview invitations "
            "will be delivered the same way.</p>")
    text = "SMTP is working. This is a test message from the staff dashboard.\n"
    return subject, text, _shell(subject, "SMTP test", html)


# ---------------------------------------------------------------- decision + follow-up emails
PASSED_DECISIONS = ("shortlisted", "hired")


def decision_kind(decision):
    """Which email a decision maps to: 'passed' (cleared the first round), 'rejected', or None (no email)."""
    if decision in PASSED_DECISIONS:
        return "passed"
    return "rejected" if decision == "rejected" else None


def default_decision_email(decision, *, position_title, org_name):
    """(subject, editable message) for a decision. The greeting and sign-off are added by render_message."""
    kind = decision_kind(decision)
    if kind == "passed":
        return (f"Update on your application for {position_title} — {org_name}",
                f"Thank you for taking the time to complete your first-round interview for the {position_title} position "
                f"at {org_name}. We are pleased to let you know that you have passed the first round.\n\n"
                "The Management will call you back soon to discuss the next steps. We will also be arranging a school meet "
                "at the college shortly, and we will let you know the date, time and venue in advance.\n\n"
                "Please keep your phone available over the coming days. If your contact details have changed, simply "
                "reply to this email.")
    if kind == "rejected":
        return (f"Your application for {position_title} — {org_name}",
                f"Thank you for applying for the {position_title} position at {org_name} and for the time you spent on "
                "your interview.\n\n"
                "After careful review by our hiring team, we have decided not to move forward with your application at "
                "this stage. This was not an easy decision, and it applies only to this role.\n\n"
                f"We appreciate your interest in {org_name} and encourage you to apply for future openings that match "
                "your skills. We wish you every success.")
    return None


def default_interview_done_email(*, position_title, org_name):
    return (f"We received your interview for {position_title} — {org_name}",
            f"Thank you for completing your interview for the {position_title} position at {org_name}. Your recording "
            "and responses have been received and will be reviewed by our hiring team.\n\n"
            "You do not need to do anything else for now. We will contact you once a decision has been made.")


def render_message(*, name, org_name, subject, message, preheader):
    """Greeting + the (editable) message paragraphs + sign-off, as (subject, text, html). Input is escaped."""
    first = (name or "there").split()[0]
    paras = [p.strip() for p in re.split(r"\n\s*\n", message or "") if p.strip()]
    body = "".join(f'<p style="margin:0 0 14px;">{escape(p).replace(chr(10), "<br>")}</p>' for p in paras)
    html = (f'<p style="margin:0 0 14px;font-size:17px;font-weight:600;">Dear {escape(first)},</p>{body}'
            f'<p style="margin:18px 0 0;">Kind regards,<br><strong>The {escape(org_name)} Recruitment Team</strong></p>')
    text = f"Dear {first},\n\n" + "\n\n".join(paras) + f"\n\nKind regards,\nThe {org_name} Recruitment Team\n"
    return subject, text, _shell(subject, preheader, html)
