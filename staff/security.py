"""Authentication, tokens, rate limiting and input hygiene for the staff system."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from functools import wraps
from urllib.parse import urlparse

from flask import g, jsonify, request

from .config import get_settings

# No 0/O/1/I so codes survive being read aloud or typed from an email.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---------------------------------------------------------------- responses
def json_error(message, status=400, **extra):
    body = {"ok": False, "error": message}
    body.update(extra)
    return jsonify(body), status


# ---------------------------------------------------------------- tokens
def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(payload, ttl_seconds):
    body = dict(payload)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    raw = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).digest()
    return f"{raw}.{_b64(sig)}"


def verify_token(token):
    try:
        raw, sig = token.split(".", 1)
        expected = hmac.new(get_settings().secret_key.encode(), raw.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        body = json.loads(_unb64(raw))
        if int(body.get("exp", 0)) < time.time():
            return None
        return body
    except Exception:
        return None


def constant_time_equal(a, b):
    return hmac.compare_digest(
        hashlib.sha256((a or "").encode()).digest(),
        hashlib.sha256((b or "").encode()).digest(),
    )


# ---------------------------------------------------------------- rate limits
class RateLimiter:
    """In-memory sliding window (the app runs a single gunicorn worker)."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key, limit, window_seconds):
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < window_seconds]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 5000:  # opportunistic cleanup
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > window_seconds]:
                    self._hits.pop(k, None)
            return True

    def reset(self, key=None):
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


limiter = RateLimiter()


def client_ip():
    """The caller's address. When the Foyer server fronts the admin API it forwards the real client IP,
    trusted only if it proves knowledge of STAFF_PROXY_KEY (so login throttling is per person, not per proxy)."""
    key = get_settings().proxy_key
    if key and hmac.compare_digest(request.headers.get("X-Staff-Proxy-Key", ""), key):
        forwarded = request.headers.get("X-Staff-Client-IP", "").strip()
        try:
            import ipaddress
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return request.remote_addr or "unknown"


def rate_limited(bucket, limit, window_seconds):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not limiter.allow(f"{bucket}:{client_ip()}", limit, window_seconds):
                return json_error("Too many requests. Please wait a moment and try again.", 429)
            return fn(*args, **kwargs)
        return wrapper
    return deco


# ---------------------------------------------------------------- admin session
SESSION_COOKIE = "staff_session"


def is_https():
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"


def issue_admin_session(username):
    s = get_settings()
    csrf = secrets.token_urlsafe(24)
    token = sign_token({"u": username, "csrf": csrf, "scope": "admin"}, s.session_hours * 3600)
    return token, csrf


def current_admin():
    """Admin session from `Authorization: Bearer` (used by the Foyer dashboard, via its server-side proxy)
    or from the HttpOnly cookie (used by the dashboard served directly at /staff)."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        body = verify_token(header[7:])
        return dict(body, via="bearer") if body and body.get("scope") == "admin" else None
    token = request.cookies.get(SESSION_COOKIE, "")
    body = verify_token(token) if token else None
    if body and body.get("scope") == "admin":
        return dict(body, via="cookie")
    return None


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        s = get_settings()
        if not s.admin_configured:
            return json_error("Admin access is not configured on the server.", 503)
        admin = current_admin()
        if not admin:
            return json_error("Please sign in.", 401)
        # Cookies are sent automatically by browsers, so cookie sessions need the CSRF header;
        # a bearer token is never attached implicitly, so it is not CSRF-able.
        if request.method not in ("GET", "HEAD", "OPTIONS") and admin["via"] == "cookie":
            sent = request.headers.get("X-CSRF-Token", "")
            if not sent or not hmac.compare_digest(sent, admin.get("csrf", "")):
                return json_error("Security check failed. Reload the page and try again.", 403)
        g.admin = admin
        return fn(*args, **kwargs)
    return wrapper


MEDIA_TTL_SECONDS = 3600


def media_url_token(kind, subject):
    return sign_token({"scope": "media", "kind": kind, "sub": subject}, MEDIA_TTL_SECONDS)


def admin_or_media(kind, param):
    """For media endpoints (<video>/<img>/download links cannot send an Authorization header): accept a
    normal admin session, or a short-lived `st` token minted by an admin and bound to ONE resource + kind."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            st = request.args.get("st", "")
            if st:
                body = verify_token(st)
                if (body and body.get("scope") == "media" and body.get("kind") == kind
                        and body.get("sub") == kwargs.get(param)):
                    g.admin = {"u": "media", "via": "media"}
                    return fn(*args, **kwargs)
                return json_error("This link has expired. Reload the page.", 401)
            return admin_required(fn)(*args, **kwargs)
        return wrapper
    return deco


# ---------------------------------------------------------------- codes
def generate_code():
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"


def normalize_code(raw):
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    if len(cleaned) != 8 or any(c not in CODE_ALPHABET for c in cleaned):
        return None
    return f"{cleaned[:4]}-{cleaned[4:]}"


# ---------------------------------------------------------------- input hygiene
def clean_str(value, max_len=200, multiline=False):
    if value is None:
        return ""
    text = str(value)
    text = CONTROL_RE.sub("", text)
    if not multiline:
        text = re.sub(r"[\r\n\t]+", " ", text)
    return text.strip()[:max_len]


def clean_email(value):
    email = clean_str(value, 254).lower()
    return email if EMAIL_RE.match(email) else ""


def clean_phone(value):
    text = clean_str(value, 30)
    return text if re.match(r"^[0-9+()\-.\s]{5,30}$", text) else ""


def clean_url(value):
    text = clean_str(value, 300)
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    parsed = urlparse(text)
    return text if parsed.netloc and "." in parsed.netloc else ""


def clean_list(value, max_items=30, max_len=60):
    if isinstance(value, str):
        value = re.split(r"[,\n;]", value)
    if not isinstance(value, (list, tuple)):
        return []
    out, seen = [], set()
    for item in value:
        text = clean_str(item, max_len)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
        if len(out) >= max_items:
            break
    return out


def clean_number(value, lo, hi, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


# ---------------------------------------------------------------- headers
def apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=()")
    if is_https():
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    # API responses carry personal data (names, transcripts): never let a browser or proxy cache them.
    if request.path.startswith(("/staff/api", "/api/recruit")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self' blob: data:; "
    "worker-src 'self' blob:; "
    "font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
