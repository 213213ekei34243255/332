"""Business logic for the staff recruitment workflow.

Applicant Enrollment -> Job Selection -> Interview Code -> Email -> Face
Verification -> AI Interview -> Recording -> AI Evaluation -> Admin Review.
"""
import io
import logging
import re
import threading
import time
import zipfile
from datetime import datetime, timezone

from . import mailer
from .db import jdump, new_id, now_ms
from .engine import CANDIDATE_ANSWER_KINDS, InterviewEngine
from .gemini import AIError
from .security import (clean_email, clean_list, clean_number, clean_phone, clean_str, clean_url,
                       generate_code, normalize_code, sign_token)
from .storage import RESUME_MIMES, VIDEO_MIMES

log = logging.getLogger("staff.services")

EDU_RANK = {"": 0, "any": 0, "high_school": 1, "diploma": 2, "bachelor": 3, "master": 4, "doctorate": 5}
EDU_LABEL = {"high_school": "High school", "diploma": "Diploma", "bachelor": "Bachelor's degree",
             "master": "Master's degree", "doctorate": "Doctorate"}
EMPLOYMENT_TYPES = ("Full-time", "Part-time", "Contract", "Internship")
ACTIVE_ENROLLMENT = ("issued", "opened", "verified", "in_progress")


class ServiceError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra


def _in(ids):
    return ",".join("?" for _ in ids)


class Services:
    def __init__(self, db, storage, mail, ai, settings):
        self.db, self.storage, self.mail, self.ai, self.s = db, storage, mail, ai, settings
        self.engine = InterviewEngine(ai, settings.org_name)
        self._eval_lock = threading.Lock()
        self._evaluating = set()

    # =================================================================== positions
    def normalize_position(self, data, existing=None):
        title = clean_str(data.get("title"), 120)
        if not title:
            raise ServiceError("A position title is required.")
        old = (existing or {}).get("config") or {}
        elig_in = data.get("eligibility") or {}
        min_edu = clean_str(elig_in.get("min_education"), 20)
        if min_edu not in EDU_RANK:
            min_edu = ""
        # keep question ids stable across edits so past evaluations still line up
        used = {q.get("id") for q in (data.get("questions") or []) if q.get("id")}
        counter = max([int(re.sub(r"\D", "", i) or 0) for i in used | {q.get("id", "q0") for q in old.get("questions", [])}] + [0])
        questions = []
        for q in (data.get("questions") or [])[:25]:
            text = clean_str(q.get("text"), 500, multiline=True)
            if not text:
                continue
            qid = clean_str(q.get("id"), 20)
            if not re.match(r"^q\d+$", qid) or any(x["id"] == qid for x in questions):
                counter += 1
                qid = f"q{counter}"
            questions.append({"id": qid, "text": text, "intent": clean_str(q.get("intent"), 300),
                              "required": bool(q.get("required", True)),
                              "weight": int(clean_number(q.get("weight"), 1, 5, 3)),
                              "rubric": clean_str(q.get("rubric"), 600, multiline=True)})
        criteria = []
        for i, c in enumerate((data.get("criteria") or [])[:12], 1):
            label = clean_str(c.get("label"), 80)
            if label:
                criteria.append({"id": clean_str(c.get("id"), 20) or f"c{i}", "label": label,
                                 "description": clean_str(c.get("description"), 300),
                                 "weight": int(clean_number(c.get("weight"), 1, 5, 3))})
        iv = data.get("interview") or {}
        tone = clean_str(iv.get("tone"), 30)
        difficulty = clean_str(iv.get("difficulty"), 30)
        interview = {
            "duration_minutes": int(clean_number(iv.get("duration_minutes"), 5, 45, 15)),
            "language": clean_str(iv.get("language"), 30) or "English",
            "tone": tone if tone in ("friendly-professional", "formal", "casual") else "friendly-professional",
            "difficulty": difficulty if difficulty in ("standard", "challenging", "easy") else "standard",
            "persona_name": clean_str(iv.get("persona_name"), 30) or "Noah",
            "speech_lang": clean_str(iv.get("speech_lang"), 12) or "en-IN",
            "allow_typing": bool(iv.get("allow_typing", True)),
        }
        config = {
            "required_skills": clean_list(data.get("required_skills")),
            "nice_to_have": clean_list(data.get("nice_to_have")),
            "seniority": clean_str(data.get("seniority"), 40),
            "eligibility": {
                "min_experience_years": clean_number(elig_in.get("min_experience_years"), 0, 50, 0),
                "min_education": min_edu,
                "must_have_skills": clean_list(elig_in.get("must_have_skills")),
                "notes": clean_str(elig_in.get("notes"), 600, multiline=True),
            },
            "objectives": clean_str(data.get("objectives"), 1500, multiline=True),
            "questions": questions,
            "criteria": criteria,
            "interview": interview,
            "auto_invite": bool(data.get("auto_invite", False)),
        }
        employment = clean_str(data.get("employment_type"), 30)
        status = clean_str(data.get("status"), 10)
        return {
            "title": title,
            "department": clean_str(data.get("department"), 80),
            "location": clean_str(data.get("location"), 80),
            "employment_type": employment if employment in EMPLOYMENT_TYPES else "Full-time",
            "description": clean_str(data.get("description"), 6000, multiline=True),
            "status": status if status in ("open", "closed") else "open",
            "config": config,
        }

    def create_position(self, data):
        fields = self.normalize_position(data)
        pid = new_id("pos")
        now = now_ms()
        self.db.insert("positions", dict(fields, id=pid, created_at=now, updated_at=now))
        return self.db.get("positions", pid)

    def update_position(self, pid, data):
        existing = self.db.get("positions", pid)
        if not existing:
            raise ServiceError("Position not found.", 404)
        self.db.update("positions", pid, dict(self.normalize_position(data, existing), updated_at=now_ms()))
        return self.db.get("positions", pid)

    def delete_position(self, pid):
        n = self.db.one("SELECT COUNT(*) AS n FROM applications WHERE position_id = ?", (pid,))["n"]
        if n:
            self.db.update("positions", pid, {"status": "closed", "updated_at": now_ms()})
            return {"deleted": False, "closed": True, "applications": n}
        self.db.delete("positions", pid)
        return {"deleted": True, "closed": False, "applications": 0}

    def list_positions(self, only_open=False):
        rows = self.db.all("SELECT * FROM positions" + (" WHERE status = 'open'" if only_open else "")
                           + " ORDER BY created_at DESC", table="positions")
        counts = {r["position_id"]: r for r in self.db.all(
            "SELECT position_id, COUNT(*) AS apps FROM applications GROUP BY position_id")}
        for r in rows:
            r["application_count"] = (counts.get(r["id"]) or {}).get("apps", 0)
        return rows

    @staticmethod
    def public_position(p):
        c = p.get("config") or {}
        e = c.get("eligibility") or {}
        return {"id": p["id"], "title": p["title"], "department": p.get("department"), "location": p.get("location"),
                "employment_type": p.get("employment_type"), "description": p.get("description"),
                "seniority": c.get("seniority"), "required_skills": c.get("required_skills", []),
                "nice_to_have": c.get("nice_to_have", []),
                "requirements": {"min_experience_years": e.get("min_experience_years", 0),
                                 "min_education": e.get("min_education", ""),
                                 "must_have_skills": e.get("must_have_skills", []), "notes": e.get("notes", "")},
                "duration_minutes": (c.get("interview") or {}).get("duration_minutes", 15)}

    # =================================================================== applications
    def evaluate_eligibility(self, position, profile, resume_text):
        e = (position.get("config") or {}).get("eligibility") or {}
        checks = []
        min_exp = float(e.get("min_experience_years") or 0)
        if min_exp > 0:
            exp = profile.get("experience_years")
            ok = exp is not None and float(exp) >= min_exp
            checks.append({"name": "Experience", "passed": ok,
                           "detail": f"{exp if exp is not None else 'not stated'} yrs (needs {min_exp:g}+)"})
        min_edu = e.get("min_education") or ""
        if EDU_RANK.get(min_edu, 0) > 0:
            ok = EDU_RANK.get(profile.get("education") or "", 0) >= EDU_RANK[min_edu]
            checks.append({"name": "Education", "passed": ok,
                           "detail": f"{EDU_LABEL.get(profile.get('education') or '', 'not stated')} (needs {EDU_LABEL[min_edu]}+)"})
        must = e.get("must_have_skills") or []
        if must:
            haystack = " ".join((profile.get("skills") or []) + [resume_text or "", profile.get("cover_note") or ""]).lower()
            missing = [m for m in must if m.lower() not in haystack]
            checks.append({"name": "Must-have skills", "passed": not missing,
                           "detail": "all present" if not missing else "missing: " + ", ".join(missing)})
        passed = all(c["passed"] for c in checks)
        return {"passed": passed, "checks": checks,
                "summary": "Meets all stated requirements." if passed else "Needs review: " + "; ".join(
                    f"{c['name']} ({c['detail']})" for c in checks if not c["passed"])}

    @staticmethod
    def _extract_resume_text(data, ext):
        try:
            if ext == "txt":
                return data.decode("utf-8", "ignore")[:15000]
            if ext == "docx":
                import docx
                doc = docx.Document(io.BytesIO(data))
                return "\n".join(p.text for p in doc.paragraphs)[:15000]
            if ext == "pdf":
                try:
                    from pypdf import PdfReader
                except ImportError:
                    from PyPDF2 import PdfReader
                reader = PdfReader(io.BytesIO(data))
                return "\n".join((pg.extract_text() or "") for pg in reader.pages[:12])[:15000]
        except Exception:
            log.warning("resume text extraction failed for .%s", ext)
        return ""

    def _validate_resume(self, filename, data):
        if not data:
            return None, None
        if len(data) > 5 * 1024 * 1024:
            raise ServiceError("The resume is larger than 5 MB.")
        ext = (filename or "").rsplit(".", 1)[-1].lower()
        if ext == "pdf" and data[:5] == b"%PDF-":
            return "pdf", "application/pdf"
        if ext == "docx" and data[:2] == b"PK" and zipfile.is_zipfile(io.BytesIO(data)):
            return "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if ext == "txt" and b"\x00" not in data[:2000]:
            return "txt", "text/plain"
        raise ServiceError("Please upload your resume as a PDF, DOCX or TXT file.")

    def build_profile(self, data):
        exp = clean_number(data.get("experience_years"), 0, 60, None)
        edu = clean_str(data.get("education"), 20)
        return {
            "experience_years": exp,
            "education": edu if edu in EDU_RANK and edu not in ("", "any") else "",
            "current_title": clean_str(data.get("current_title"), 100),
            "current_company": clean_str(data.get("current_company"), 100),
            "location": clean_str(data.get("location"), 100),
            "skills": clean_list(data.get("skills")),
            "notice_period": clean_str(data.get("notice_period"), 60),
            "linkedin": clean_url(data.get("linkedin")),
            "portfolio": clean_url(data.get("portfolio")),
            "cover_note": clean_str(data.get("cover_note"), 2000, multiline=True),
        }

    def apply(self, data, resume=None, ip="", base_url="", actor="applicant"):
        name = clean_str(data.get("full_name"), 100)
        email = clean_email(data.get("email"))
        if len(name) < 2:
            raise ServiceError("Please enter your full name.")
        if not email:
            raise ServiceError("Please enter a valid email address.")
        position = self.db.get("positions", clean_str(data.get("position_id"), 40))
        if not position or (position["status"] != "open" and actor == "applicant"):
            raise ServiceError("That position is not open for applications.", 404)
        profile = self.build_profile(data)
        if profile["experience_years"] is None:
            raise ServiceError("Please tell us your years of experience (0 if none).")

        dup = self.db.one("SELECT id FROM applications WHERE position_id = ? AND email = ? AND created_at > ?",
                          (position["id"], email, now_ms() - 90 * 86400_000), "applications")
        if dup and actor == "applicant":
            return {"id": dup["id"], "duplicate": True, "auto_invited": False}

        app_id = new_id("app")
        resume_file = resume_mime = None
        resume_text = ""
        if resume and resume[1]:
            ext, resume_mime = self._validate_resume(resume[0], resume[1])
            resume_text = self._extract_resume_text(resume[1], ext)
            self.storage.save_resume(app_id, ext, resume[1])
            resume_file = f"{app_id}.{ext}"
        eligibility = self.evaluate_eligibility(position, profile, resume_text)
        now = now_ms()
        self.db.insert("applications", {
            "id": app_id, "position_id": position["id"], "full_name": name, "email": email,
            "phone": clean_phone(data.get("phone")), "profile": profile, "resume_file": resume_file,
            "resume_mime": resume_mime, "resume_text": resume_text, "eligibility": eligibility,
            "status": "submitted" if eligibility["passed"] else "needs_review", "decision": None,
            "admin_notes": "", "ip": ip, "created_at": now, "updated_at": now})
        application = self.db.get("applications", app_id)

        if actor == "applicant":
            subject, text, html = mailer.render_received(name=name, position_title=position["title"],
                                                         org_name=self.s.org_name)
            self.mail.enqueue(to_addr=email, subject=subject, text_body=text, html_body=html,
                              kind="received", application_id=app_id)
            if self.s.notify_email:
                subject, text, html = mailer.render_admin_notice(
                    applicant=name, position_title=position["title"], eligibility_text=eligibility["summary"],
                    dashboard_url=(self.s.public_url or base_url.rstrip("/")) + "/staff", org_name=self.s.org_name)
                self.mail.enqueue(to_addr=self.s.notify_email, subject=subject, text_body=text, html_body=html,
                                  kind="admin_notice", application_id=app_id)
        auto = False
        if actor == "applicant" and position["config"].get("auto_invite") and eligibility["passed"]:
            try:
                self.invite(app_id, actor="system", base_url=base_url)
                auto = True
            except Exception:
                log.exception("auto-invite failed")
        return {"id": app_id, "duplicate": False, "auto_invited": auto, "application": application}

    def update_application(self, app_id, data):
        application = self.db.get("applications", app_id)
        if not application:
            raise ServiceError("Application not found.", 404)
        values = {"updated_at": now_ms()}
        if "decision" in data:
            decision = clean_str(data["decision"], 20)
            if decision not in ("", "shortlisted", "on_hold", "rejected", "hired"):
                raise ServiceError("Unknown decision.")
            values["decision"] = decision or None
        if "admin_notes" in data:
            values["admin_notes"] = clean_str(data["admin_notes"], 4000, multiline=True)
        self.db.update("applications", app_id, values)
        return self.db.get("applications", app_id)

    def delete_application(self, app_id):
        application = self.db.get("applications", app_id)
        if not application:
            raise ServiceError("Application not found.", 404)
        for iv in self.db.all("SELECT id FROM interviews WHERE application_id = ?", (app_id,)):
            self.storage.delete_interview_files(iv["id"])
            self.db.run("DELETE FROM turns WHERE interview_id = ?", (iv["id"],))
            self.db.run("DELETE FROM recordings WHERE interview_id = ?", (iv["id"],))
            self.db.delete("interviews", iv["id"])
        for en in self.db.all("SELECT id FROM enrollments WHERE application_id = ?", (app_id,)):
            self.storage.delete_photo(en["id"])
        self.db.run("DELETE FROM enrollments WHERE application_id = ?", (app_id,))
        self.db.run("DELETE FROM emails WHERE application_id = ?", (app_id,))
        self.storage.delete_resume(app_id, application.get("resume_file"))
        self.db.delete("applications", app_id)

    # =================================================================== enrollments / invites
    def invite(self, app_id, actor="admin", expires_days=None, message="", base_url="", resend_only=False):
        application = self.db.get("applications", app_id)
        if not application:
            raise ServiceError("Application not found.", 404)
        position = self.db.get("positions", application["position_id"])
        now = now_ms()
        active = self.db.one(
            "SELECT * FROM enrollments WHERE application_id = ? AND status IN (?,?,?,?) AND expires_at > ? "
            "ORDER BY created_at DESC", (app_id,) + ACTIVE_ENROLLMENT + (now,), "enrollments")
        used = self.db.one("SELECT id FROM enrollments WHERE application_id = ? AND status = 'completed'", (app_id,))
        if used and not resend_only:
            raise ServiceError("This applicant has already completed an interview. Delete or reset it first.", 409)
        if active and resend_only:
            enrollment = active
        else:
            if active:  # a fresh code replaces the old one
                self.db.update("enrollments", active["id"], {"status": "revoked"})
            days = int(clean_number(expires_days, 1, 30, self.s.code_ttl_days))
            enrollment = None
            for _ in range(8):
                code = generate_code()
                if self.db.one("SELECT id FROM enrollments WHERE code = ?", (code,)):
                    continue
                eid = new_id("enr")
                try:
                    self.db.insert("enrollments", {
                        "id": eid, "application_id": app_id, "code": code, "status": "issued",
                        "expires_at": now + days * 86400_000, "created_at": now, "created_by": actor,
                        "face_attempts": 0, "meta": {}})
                    enrollment = self.db.get("enrollments", eid)
                    break
                except Exception:
                    log.warning("enrollment code collision, retrying")
            if not enrollment:
                raise ServiceError("Could not generate a unique code. Please try again.", 500)
        base = (self.s.public_url or base_url).rstrip("/")
        link = f"{base}/join?code={enrollment['code']}"
        expires_text = datetime.fromtimestamp(enrollment["expires_at"] / 1000, tz=timezone.utc).strftime("%d %B %Y, %H:%M UTC")
        duration = ((position.get("config") or {}).get("interview") or {}).get("duration_minutes", 15)
        subject, text, html = mailer.render_invite(
            name=application["full_name"], position_title=position["title"], code=enrollment["code"], link=link,
            expires_text=expires_text, duration_minutes=duration, org_name=self.s.org_name,
            support_email=self.s.support_email, custom_message=clean_str(message, 600, multiline=True))
        email_id = self.mail.enqueue(to_addr=application["email"], subject=subject, text_body=text, html_body=html,
                                     kind="invite", application_id=app_id, enrollment_id=enrollment["id"])
        if application["status"] in ("submitted", "needs_review", "invited"):
            self.db.update("applications", app_id, {"status": "invited", "updated_at": now_ms()})
        self.db.audit(actor, "invite", app_id, f"code {enrollment['code'][:4]}-****")
        return {"enrollment": enrollment, "email_id": email_id, "link": link}

    def revoke_enrollment(self, enrollment_id, actor="admin"):
        en = self.db.get("enrollments", enrollment_id)
        if not en:
            raise ServiceError("Enrollment not found.", 404)
        if en["status"] in ("completed", "revoked"):
            raise ServiceError(f"This code is already {en['status']}.", 409)
        self.db.update("enrollments", enrollment_id, {"status": "revoked"})
        self.db.audit(actor, "revoke", enrollment_id)

    def reset_face(self, enrollment_id, actor="admin"):
        en = self.db.get("enrollments", enrollment_id)
        if not en:
            raise ServiceError("Enrollment not found.", 404)
        meta = dict(en["meta"] or {})
        meta.pop("face", None)
        status = "issued" if en["status"] in ("opened", "verified", "issued") else en["status"]
        self.db.update("enrollments", enrollment_id, {"face_attempts": 0, "meta": meta, "status": status,
                                                      "face_verified_at": None})
        self.db.audit(actor, "reset_face", enrollment_id)

    @staticmethod
    def effective_enrollment_status(en):
        # An interview already under way may be resumed after the code's expiry date.
        if en["status"] in ("issued", "opened", "verified") and en["expires_at"] < now_ms():
            return "expired"
        return en["status"]

    def lookup_code(self, raw_code):
        code = normalize_code(raw_code)
        if not code:
            raise ServiceError("That code doesn't look right. Codes look like ABCD-2345.", 404)
        en = self.db.one("SELECT * FROM enrollments WHERE code = ?", (code,), "enrollments")
        if not en:
            raise ServiceError("We couldn't find that code. Please check your email and try again.", 404)
        status = self.effective_enrollment_status(en)
        if status == "revoked":
            raise ServiceError("This code is no longer valid. Please contact the hiring team.", 410)
        if status == "expired":
            raise ServiceError("This code has expired. Please contact the hiring team for a new one.", 410)
        if status == "completed":
            raise ServiceError("This interview has already been completed. Thank you!", 409)
        if status == "issued":
            self.db.update("enrollments", en["id"], {"status": "opened", "opened_at": now_ms()})
            en["status"] = "opened"
        application = self.db.get("applications", en["application_id"])
        position = self.db.get("positions", application["position_id"])
        token = sign_token({"scope": "enroll", "eid": en["id"]}, 6 * 3600)
        return en, application, position, token

    # =================================================================== face verification
    FACE_TASKS = ["blink", "smile", "open_mouth", "turn_head"]

    def face_challenge(self, enrollment):
        if enrollment["face_attempts"] >= self.s.face_max_attempts:
            raise ServiceError("Too many verification attempts. Please contact the hiring team.", 429)
        import secrets
        tasks = ["center"] + secrets.SystemRandom().sample(self.FACE_TASKS, 2)
        meta = dict(enrollment["meta"] or {})
        meta["face"] = {"nonce": secrets.token_urlsafe(16), "tasks": tasks, "issued_at": now_ms()}
        self.db.update("enrollments", enrollment["id"], {"meta": meta})
        return {"nonce": meta["face"]["nonce"], "tasks": tasks}

    def face_submit(self, enrollment, photo, result):
        meta = dict(enrollment["meta"] or {})
        issued = (meta.get("face") or {})
        attempts = enrollment["face_attempts"] + 1
        self.db.update("enrollments", enrollment["id"], {"face_attempts": attempts})

        def fail(msg):
            raise ServiceError(msg, 422, attempts_left=max(0, self.s.face_max_attempts - attempts))

        if attempts > self.s.face_max_attempts:
            raise ServiceError("Too many verification attempts. Please contact the hiring team.", 429)
        if not issued or issued.get("nonce") != (result or {}).get("nonce"):
            fail("The verification session expired. Please start the face check again.")
        if now_ms() - issued["issued_at"] > 10 * 60_000:
            fail("The verification session timed out. Please try again.")
        done = {s.get("name"): s for s in (result or {}).get("steps", []) if s.get("passed")}
        missing = [t for t in issued["tasks"] if t not in done]
        if missing:
            fail("Not every verification step was completed. Please try again.")
        elapsed = now_ms() - issued["issued_at"]
        if elapsed < 2500:
            fail("The verification finished too quickly. Please try again.")
        if len(photo) < 5_000 or len(photo) > 2_000_000 or photo[:3] != b"\xff\xd8\xff":
            fail("We couldn't read the photo from your camera. Please try again.")
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(photo))
            img.verify()
            if img.size[0] < 200 or img.size[1] < 200:
                fail("The camera image is too small. Please try again.")
        except ServiceError:
            raise
        except Exception:
            fail("We couldn't read the photo from your camera. Please try again.")

        server_check = {"status": "skipped"}
        if self.ai.configured:
            try:
                check = self.ai.check_face(photo)
                server_check = {"status": "ok", "face_count": check.face_count,
                                "live": check.single_live_person, "notes": check.notes[:200]}
                if check.face_count != 1:
                    fail("We need exactly one face clearly in view. Please make sure you're alone and try again.")
                if check.screen_or_printed_photo_suspected or not check.single_live_person:
                    fail("We couldn't confirm a live person on camera. Please face the camera directly in good lighting.")
                if not check.face_clearly_visible:
                    fail("Your face isn't clearly visible. Please improve the lighting and remove any obstructions.")
            except AIError:
                server_check = {"status": "unavailable"}
        self.storage.save_photo(enrollment["id"], photo)
        meta["face"] = {"verified": True, "tasks": issued["tasks"], "elapsed_ms": elapsed,
                        "detector": (result or {}).get("detector", "mediapipe"), "server_check": server_check,
                        "metrics": (result or {}).get("metrics", {})}
        self.db.update("enrollments", enrollment["id"], {
            "meta": meta, "status": "verified" if enrollment["status"] != "in_progress" else "in_progress",
            "face_verified_at": now_ms()})
        self.db.update("applications", enrollment["application_id"], {"status": "face_verified", "updated_at": now_ms()})
        return meta["face"]

    # =================================================================== interviews
    def _turns(self, interview_id):
        return self.db.all("SELECT * FROM turns WHERE interview_id = ? ORDER BY idx ASC", (interview_id,), "turns")

    def _interview_for(self, enrollment):
        return self.db.one("SELECT * FROM interviews WHERE enrollment_id = ? ORDER BY created_at DESC",
                           (enrollment["id"],), "interviews")

    def start_interview(self, enrollment, application, position):
        if not enrollment.get("face_verified_at"):
            raise ServiceError("Face verification must be completed before the interview.", 403)
        iv = self._interview_for(enrollment)
        settings = None
        if iv and iv["status"] != "in_progress":
            raise ServiceError("This interview has already been completed.", 409)
        if iv:  # resume after a dropped connection / reload
            if iv["resume_count"] >= self.s.max_resumes:
                raise ServiceError("This interview was interrupted too many times. Please contact the hiring team.", 409)
            self.db.update("interviews", iv["id"], {"resume_count": iv["resume_count"] + 1, "updated_at": now_ms()})
            turns = self._turns(iv["id"])
            segment = len(self.db.all("SELECT id FROM recordings WHERE interview_id = ?", (iv["id"],)))
            return self._start_payload(iv, turns, position, resumed=True, segment=segment)
        if not self.ai.configured:
            raise ServiceError("The AI interviewer is not available right now. Please try again shortly.", 503)
        state, opening = self.engine.start(position, application)
        iv_id, now = new_id("int"), now_ms()
        self.db.insert("interviews", {
            "id": iv_id, "enrollment_id": enrollment["id"], "application_id": application["id"],
            "position_id": position["id"], "status": "in_progress", "state": state,
            "face": (enrollment["meta"] or {}).get("face", {}), "events": [], "resume_count": 0,
            "started_at": now, "created_at": now, "updated_at": now})
        self._add_turn(iv_id, 0, "ai", opening["reply"], {"move": "opening", "topic_id": "bg1"})
        self.db.update("enrollments", enrollment["id"], {"status": "in_progress"})
        self.db.update("applications", application["id"], {"status": "interviewing", "updated_at": now})
        iv = self.db.get("interviews", iv_id)
        return self._start_payload(iv, self._turns(iv_id), position, resumed=False, segment=0)

    def _start_payload(self, iv, turns, position, resumed, segment):
        cfg = (position.get("config") or {}).get("interview") or {}
        st = iv["state"] or {}
        return {"interview_id": iv["id"], "resumed": resumed, "segment": segment,
                "turns": [{"idx": t["idx"], "role": t["role"], "text": t["body"]} for t in turns],
                "last_ai_text": next((t["body"] for t in reversed(turns) if t["role"] == "ai"), ""),
                "elapsed_ms": now_ms() - iv["started_at"],
                "config": {"duration_minutes": cfg.get("duration_minutes", 15), "speech_lang": cfg.get("speech_lang", "en-IN"),
                           "allow_typing": cfg.get("allow_typing", True), "persona": cfg.get("persona_name", "Noah"),
                           "video_bps": self.s.video_bps, "audio_bps": self.s.audio_bps,
                           "tts": bool(self.s.tts_enabled and self.ai.configured),
                           "target_questions": (st.get("settings") or {}).get("target_questions", 10)},
                "done": bool(st.get("closing"))}

    def _add_turn(self, interview_id, idx, role, body, meta=None):
        self.db.insert("turns", {"id": new_id("trn"), "interview_id": interview_id, "idx": idx, "role": role,
                                 "body": body[:6000], "meta": meta or {}, "created_at": now_ms()})

    def handle_turn(self, enrollment, application, position, text, kind, turn_idx, meta=None):
        iv = self._interview_for(enrollment)
        if not iv:
            raise ServiceError("The interview has not started.", 409)
        if iv["status"] != "in_progress":
            raise ServiceError("The interview has ended.", 409)
        kind = kind if kind in ("answer", "typed", "silence", "repeat") else "answer"
        text = clean_str(text, 4000, multiline=True)
        if kind in CANDIDATE_ANSWER_KINDS and not text:
            raise ServiceError("Empty answer.")
        turns = self._turns(iv["id"])
        existing = next((t for t in turns if t["idx"] == turn_idx), None)
        reply_row = next((t for t in turns if t["idx"] == turn_idx + 1), None)
        if reply_row and reply_row["role"] == "ai":  # retry of a turn we already answered
            return {"reply": reply_row["body"], "end": bool((iv["state"] or {}).get("closing")),
                    "turn_idx": reply_row["idx"], "replayed": True, **self._progress(iv)}
        if turn_idx != len(turns) and not existing:
            raise ServiceError("Out of sync with the server.", 409, expected_idx=len(turns))
        prior = [t for t in turns if t["idx"] < turn_idx]
        if not existing:
            delay = clean_number((meta or {}).get("delay_ms"), 0, 3_600_000, None)
            self._add_turn(iv["id"], turn_idx, "candidate", text or "(no response)",
                           {"kind": kind, "delay_ms": delay, "stt": (meta or {}).get("stt")})
        elapsed = now_ms() - iv["started_at"]
        state, out = self.engine.turn(position, application, iv["state"], prior, text if existing is None else existing["body"],
                                      kind, elapsed)
        self._add_turn(iv["id"], turn_idx + 1, "ai", out["reply"],
                       {k: out[k] for k in ("move", "topic_id", "quality", "assessment", "flag")})
        self.db.update("interviews", iv["id"], {"state": state, "updated_at": now_ms()})
        iv["state"] = state
        return {"reply": out["reply"], "end": out["end"], "turn_idx": turn_idx + 1, "replayed": False, **self._progress(iv)}

    def _progress(self, iv):
        st = iv["state"] or {}
        settings = st.get("settings") or {}
        return {"progress": {"answered": st.get("turn_count", 0), "target": settings.get("target_questions", 10),
                             "elapsed_ms": now_ms() - iv["started_at"],
                             "duration_minutes": settings.get("duration_minutes", 15)}}

    def add_events(self, enrollment, events):
        iv = self._interview_for(enrollment)
        if not iv:
            return 0
        allowed = {"face_absent", "multiple_faces", "looking_away", "tab_hidden", "paste", "connection_lost"}
        clean = []
        for e in (events or [])[:50]:
            if e.get("type") in allowed:
                clean.append({"type": e["type"], "t": int(clean_number(e.get("t"), 0, 86_400_000, 0)),
                              "duration_ms": int(clean_number(e.get("duration_ms"), 0, 3_600_000, 0)),
                              "detail": clean_str(e.get("detail"), 120)})
        if clean:
            merged = ((iv["events"] or []) + clean)[-500:]
            self.db.update("interviews", iv["id"], {"events": merged, "updated_at": now_ms()})
        return len(clean)

    def finish_interview(self, enrollment, reason="completed"):
        iv = self._interview_for(enrollment)
        if not iv:
            raise ServiceError("The interview has not started.", 409)
        if iv["status"] == "in_progress":
            self.db.update("interviews", iv["id"], {"status": "evaluating", "ended_at": now_ms(), "updated_at": now_ms()})
            self.db.update("enrollments", enrollment["id"], {"status": "completed", "used_at": now_ms()})
            self.db.update("applications", enrollment["application_id"], {"status": "interviewed", "updated_at": now_ms()})
            if reason != "completed":
                state = iv["state"] or {}
                state["end_reason"] = reason
                self.db.update("interviews", iv["id"], {"state": state})
            threading.Thread(target=self.run_evaluation, args=(iv["id"],), daemon=True, name="staff-eval").start()
        return {"interview_id": iv["id"]}

    def run_evaluation(self, interview_id):
        with self._eval_lock:
            if interview_id in self._evaluating:
                return
            self._evaluating.add(interview_id)
        try:
            iv = self.db.get("interviews", interview_id)
            if not iv:
                return
            application = self.db.get("applications", iv["application_id"])
            position = self.db.get("positions", iv["position_id"])
            turns = self._turns(interview_id)
            try:
                result = self.engine.evaluate(position, application, iv["state"], turns, iv["events"])
            except AIError as exc:
                state = iv["state"] or {}
                state["eval_error"] = str(exc)[:200]
                self.db.update("interviews", interview_id, {"status": "evaluation_failed", "state": state,
                                                            "updated_at": now_ms()})
                log.warning("evaluation failed for %s: %s", interview_id, exc)
                return
            self.db.update("interviews", interview_id, {
                "status": "evaluated", "evaluation": result, "overall_score": result.get("overall_score"),
                "recommendation": result.get("recommendation"), "updated_at": now_ms()})
            self.db.update("applications", iv["application_id"], {"status": "evaluated", "updated_at": now_ms()})
        except Exception:
            log.exception("evaluation crashed for %s", interview_id)
        finally:
            with self._eval_lock:
                self._evaluating.discard(interview_id)

    def reevaluate(self, interview_id):
        iv = self.db.get("interviews", interview_id)
        if not iv:
            raise ServiceError("Interview not found.", 404)
        if iv["status"] == "in_progress":
            raise ServiceError("The interview is still in progress.", 409)
        self.db.update("interviews", interview_id, {"status": "evaluating", "updated_at": now_ms()})
        threading.Thread(target=self.run_evaluation, args=(interview_id,), daemon=True, name="staff-eval").start()

    def recover(self):
        """Startup sweep: finish evaluations a restart interrupted; close abandoned sessions."""
        try:
            for iv in self.db.all("SELECT id FROM interviews WHERE status = 'evaluating'"):
                threading.Thread(target=self.run_evaluation, args=(iv["id"],), daemon=True).start()
            stale = now_ms() - 3 * 3600_000
            for iv in self.db.all("SELECT * FROM interviews WHERE status = 'in_progress' AND updated_at < ?", (stale,), "interviews"):
                en = self.db.get("enrollments", iv["enrollment_id"])
                if en:
                    self.finish_interview(en, reason="abandoned")
        except Exception:
            log.exception("recovery sweep failed")

    # =================================================================== recordings
    def save_chunk(self, enrollment, segment, seq, data, mime):
        iv = self._interview_for(enrollment)
        if not iv:
            raise ServiceError("The interview has not started.", 409)
        mime = (mime or "video/webm").split(";")[0].strip().lower()
        if mime not in VIDEO_MIMES:
            raise ServiceError("Unsupported recording format.", 415)
        if not data or len(data) > 12 * 1024 * 1024:
            raise ServiceError("Bad chunk size.", 413)
        if not self.storage.has_room():
            raise ServiceError("Recording storage is full. Please contact the hiring team.", 507)
        rec = self.db.one("SELECT * FROM recordings WHERE interview_id = ? AND segment = ?", (iv["id"], segment))
        if not rec:
            rec_id = new_id("rec")
            self.db.insert("recordings", {"id": rec_id, "interview_id": iv["id"], "segment": segment,
                                          "filename": f"seg-{segment}.{VIDEO_MIMES[mime]}", "mime": mime, "size": 0,
                                          "duration_ms": 0, "chunks": 0, "status": "uploading", "created_at": now_ms()})
            rec = self.db.get("recordings", rec_id)
        if rec["status"] == "complete":
            raise ServiceError("This recording segment is already finalised.", 409)
        if seq < rec["chunks"]:
            return {"next_seq": rec["chunks"], "duplicate": True}
        if seq > rec["chunks"]:
            raise ServiceError("Chunk out of order.", 409, expected_seq=rec["chunks"])
        if rec["size"] + len(data) > self.s.max_recording_mb * 1024 * 1024:
            raise ServiceError("Recording is too large.", 413)
        size = self.storage.append_chunk(iv["id"], rec["filename"], data, reset=(seq == 0))
        self.db.update("recordings", rec["id"], {"size": size, "chunks": seq + 1})
        return {"next_seq": seq + 1, "duplicate": False}

    def complete_recording(self, enrollment, segment, total_chunks, duration_ms):
        iv = self._interview_for(enrollment)
        if not iv:
            raise ServiceError("The interview has not started.", 409)
        rec = self.db.one("SELECT * FROM recordings WHERE interview_id = ? AND segment = ?", (iv["id"], segment))
        if not rec:
            raise ServiceError("No recording was received for this segment.", 404)
        if rec["chunks"] != total_chunks:
            raise ServiceError("Some recording chunks are missing.", 409, expected_seq=rec["chunks"])
        self.db.update("recordings", rec["id"], {"status": "complete", "finalized_at": now_ms(),
                                                 "duration_ms": int(clean_number(duration_ms, 0, 86_400_000, 0))})
        return {"ok": True}

    # =================================================================== admin queries
    def stats(self):
        def count(sql, params=()):
            return self.db.one(sql, params)["n"]
        return {
            "positions_open": count("SELECT COUNT(*) AS n FROM positions WHERE status = 'open'"),
            "applications": count("SELECT COUNT(*) AS n FROM applications"),
            "awaiting_invite": count("SELECT COUNT(*) AS n FROM applications WHERE status IN ('submitted','needs_review')"),
            "invited": count("SELECT COUNT(*) AS n FROM applications WHERE status IN ('invited','face_verified')"),
            "interviewing": count("SELECT COUNT(*) AS n FROM interviews WHERE status = 'in_progress'"),
            "completed": count("SELECT COUNT(*) AS n FROM interviews WHERE status IN ('evaluating','evaluated','evaluation_failed')"),
            "to_review": count("SELECT COUNT(*) AS n FROM interviews i JOIN applications a ON a.id = i.application_id "
                               "WHERE i.status = 'evaluated' AND (a.decision IS NULL OR a.decision = '')"),
            "recordings": count("SELECT COUNT(*) AS n FROM recordings"),
            "storage_mb": self.storage.used_mb(),
        }

    def list_applications(self, position_id="", status="", q="", page=1, page_size=25):
        where, params = [], []
        if position_id:
            where.append("a.position_id = ?"); params.append(position_id)
        if status:
            if status.startswith("decision:"):
                where.append("a.decision = ?"); params.append(status.split(":", 1)[1])
            else:
                where.append("a.status = ?"); params.append(status)
        if q:
            where.append("(LOWER(a.full_name) LIKE ? OR LOWER(a.email) LIKE ?)")
            params += [f"%{q.lower()}%"] * 2
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.one(f"SELECT COUNT(*) AS n FROM applications a{clause}", params)["n"]
        rows = self.db.all(
            f"SELECT a.*, p.title AS position_title FROM applications a LEFT JOIN positions p ON p.id = a.position_id"
            f"{clause} ORDER BY a.created_at DESC LIMIT ? OFFSET ?", params + [page_size, (page - 1) * page_size],
            "applications")
        ids = [r["id"] for r in rows]
        if ids:
            ens = self.db.all(f"SELECT * FROM enrollments WHERE application_id IN ({_in(ids)}) ORDER BY created_at ASC", ids, "enrollments")
            ivs = self.db.all(f"SELECT id, application_id, status, overall_score, recommendation FROM interviews "
                              f"WHERE application_id IN ({_in(ids)}) ORDER BY created_at ASC", ids)
            for r in rows:
                mine = [e for e in ens if e["application_id"] == r["id"]]
                latest = mine[-1] if mine else None
                r["enrollment"] = ({"id": latest["id"], "code": latest["code"],
                                    "status": self.effective_enrollment_status(latest),
                                    "expires_at": latest["expires_at"]} if latest else None)
                r["interview"] = next((i for i in reversed(ivs) if i["application_id"] == r["id"]), None)
                r.pop("resume_text", None)
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def application_detail(self, app_id):
        a = self.db.get("applications", app_id)
        if not a:
            raise ServiceError("Application not found.", 404)
        a["position"] = self.db.get("positions", a["position_id"])
        a["enrollments"] = [dict(e, status=self.effective_enrollment_status(e)) for e in self.db.all(
            "SELECT * FROM enrollments WHERE application_id = ? ORDER BY created_at DESC", (app_id,), "enrollments")]
        a["emails"] = self.db.all("SELECT * FROM emails WHERE application_id = ? ORDER BY created_at DESC", (app_id,))
        a["interviews"] = self.db.all(
            "SELECT id, status, overall_score, recommendation, started_at, ended_at FROM interviews "
            "WHERE application_id = ? ORDER BY created_at DESC", (app_id,))
        a["has_resume"] = bool(a.get("resume_file"))
        a["resume_excerpt"] = (a.get("resume_text") or "")[:3000]
        a.pop("resume_text", None)
        return a

    def list_interviews(self, position_id="", status="", q="", page=1, page_size=25):
        where, params = [], []
        if position_id:
            where.append("i.position_id = ?"); params.append(position_id)
        if status:
            where.append("i.status = ?"); params.append(status)
        if q:
            where.append("(LOWER(a.full_name) LIKE ? OR LOWER(a.email) LIKE ? OR UPPER(e.code) LIKE ?)")
            params += [f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.upper()}%"]
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        base = ("FROM interviews i JOIN applications a ON a.id = i.application_id "
                "JOIN positions p ON p.id = i.position_id JOIN enrollments e ON e.id = i.enrollment_id")
        total = self.db.one(f"SELECT COUNT(*) AS n {base}{clause}", params)["n"]
        rows = self.db.all(
            f"SELECT i.id, i.status, i.overall_score, i.recommendation, i.started_at, i.ended_at, i.application_id, "
            f"i.position_id, a.full_name, a.email, a.decision, p.title AS position_title, e.code AS code, "
            f"e.face_verified_at AS face_verified_at {base}{clause} ORDER BY i.started_at DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size])
        ids = [r["id"] for r in rows]
        if ids:
            recs = self.db.all(f"SELECT interview_id, COUNT(*) AS n, SUM(size) AS size, SUM(duration_ms) AS dur "
                               f"FROM recordings WHERE interview_id IN ({_in(ids)}) GROUP BY interview_id", ids)
            by = {r["interview_id"]: r for r in recs}
            for r in rows:
                rec = by.get(r["id"])
                # SUM() comes back as Decimal on Postgres: coerce to plain numbers for JSON
                r["recordings"] = int(rec["n"]) if rec else 0
                r["recording_mb"] = round(int(rec["size"] or 0) / 1048576, 1) if rec else 0
                r["recording_seconds"] = int(int(rec["dur"] or 0) / 1000) if rec else 0
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def interview_detail(self, interview_id):
        iv = self.db.get("interviews", interview_id)
        if not iv:
            raise ServiceError("Interview not found.", 404)
        iv["application"] = self.application_detail(iv["application_id"])
        iv["application"].pop("position", None)
        iv["position"] = self.db.get("positions", iv["position_id"])
        iv["turns"] = [{"idx": t["idx"], "role": t["role"], "text": t["body"], "meta": t["meta"],
                        "t": t["created_at"] - iv["started_at"]} for t in self._turns(interview_id)]
        iv["recordings"] = self.db.all("SELECT * FROM recordings WHERE interview_id = ? ORDER BY segment ASC", (interview_id,))
        en = self.db.get("enrollments", iv["enrollment_id"])
        iv["enrollment"] = {"id": en["id"], "code": en["code"], "face_verified_at": en["face_verified_at"],
                            "has_photo": self.storage.photo_path(en["id"]).exists(),
                            "face": (en["meta"] or {}).get("face", {})} if en else None
        state = iv.get("state") or {}
        iv["coverage"] = state.get("topics", [])
        iv["flags"] = state.get("flags", [])
        iv["plan_summary"] = (state.get("plan") or {}).get("candidate_summary", "")
        iv.pop("state", None)
        return iv

    def delete_recording(self, rec_id, actor):
        rec = self.db.get("recordings", rec_id)
        if not rec:
            raise ServiceError("Recording not found.", 404)
        try:
            self.storage.recording_path(rec["interview_id"], rec["filename"]).unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
        self.db.delete("recordings", rec_id)
        self.db.audit(actor, "delete_recording", rec_id)
