"""Environment-driven settings for the staff recruitment system.

Everything secret (Rexy model key, SMTP password, admin password) comes from
environment variables -- on Render these are the "Environment" tab values.
"""
import hashlib
import os
from pathlib import Path


def _env(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _flag(*names, default=False):
    raw = _env(*names, default="")
    if raw == "":
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _int(*names, default=0):
    try:
        return int(_env(*names, default=str(default)))
    except ValueError:
        return default


def is_persistent(path, *, windows=None, ismount=None):
    """True when `path` is on a mounted volume (e.g. a Render persistent disk) rather than the container's
    temporary filesystem, which is erased on every deploy, restart and (free tier) spin-down.
    Always True on Windows, where this only ever runs for local development."""
    if (os.name == "nt") if windows is None else windows:
        return True
    ismount = ismount or os.path.ismount
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    for q in [p, *p.parents]:
        if q == Path(q.anchor):          # reached the filesystem root, which is always a mount
            break
        if ismount(str(q)):
            return True
    return False


def _pick_data_dir():
    explicit = _env("STAFF_DATA_DIR")
    if explicit:
        return Path(explicit)
    # Render persistent disks are mounted under /var/data (start.sh already
    # keeps the llama model there), so recordings survive deploys.
    persistent = Path("/var/data")
    if persistent.is_dir() and os.access(persistent, os.W_OK):
        return persistent / "staff"
    return Path(__file__).resolve().parent.parent / "staff_data"


class Settings:
    def __init__(self):
        self.app_dir = Path(__file__).resolve().parent.parent
        self.web_dir = Path(__file__).resolve().parent / "web"

        # ---- storage -------------------------------------------------
        self.data_dir = _pick_data_dir()
        self.database_url = _env("DATABASE_URL")
        self.sqlite_path = self.data_dir / "staff.db"
        self.pg_sslmode = _env("PGSSLMODE", default="prefer")

        # ---- admin auth ----------------------------------------------
        self.admin_password = _env("STAFF_ADMIN_PASSWORD", "ADMIN_API_KEY")
        self.admin_username = _env("STAFF_ADMIN_USERNAME", default="admin")
        self.session_hours = _int("STAFF_SESSION_HOURS", default=12)
        # Shared secret the Foyer server sends so we can trust the end-user IP it forwards (optional).
        self.proxy_key = _env("STAFF_PROXY_KEY")
        explicit_secret = _env("STAFF_SECRET_KEY", "SECRET_KEY")
        if explicit_secret:
            self.secret_key = explicit_secret
        else:
            # Deterministic so sessions/interview tokens survive restarts;
            # rotates automatically when the admin password changes.
            seed = "staff-secret:" + (self.admin_password or "unconfigured")
            self.secret_key = hashlib.sha256(seed.encode()).hexdigest()

        # ---- Rexy model ------------------------------------------------
        # REXY_* is the documented name; the older variable names keep working so no deploy breaks.
        self.gemini_api_key = _env("REXY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        self.gemini_base_url = _env("REXY_BASE_URL", "GEMINI_BASE_URL")  # optional proxy / test double
        self.ai_mock = _flag("STAFF_AI_MOCK")
        self.model_turn = _env("STAFF_MODEL_TURN", "REXY_MODEL_TURN", "GEMINI_MODEL_TURN")
        self.model_eval = _env("STAFF_MODEL_EVAL", "REXY_MODEL_EVAL", "GEMINI_MODEL_EVAL")
        self.model_tts = _env("STAFF_MODEL_TTS", "REXY_MODEL_TTS", "GEMINI_MODEL_TTS")
        self.model_stt = _env("STAFF_MODEL_STT", "REXY_MODEL_STT", "GEMINI_MODEL_STT")
        self.tts_enabled = _flag("STAFF_TTS_ENABLED", default=True)
        self.tts_voice = _env("STAFF_TTS_VOICE", default="Kore")

        # ---- SMTP (Google) -------------------------------------------
        self.smtp_host = _env("SMTP_HOST", default="smtp.gmail.com")
        self.smtp_port = _int("SMTP_PORT", default=465)
        self.smtp_user = _env("SMTP_USER", "SMTP_USERNAME")
        # Google shows app passwords as "abcd efgh ijkl mnop"; spaces are cosmetic.
        self.smtp_password = "".join(
            _env("SMTP_PASSWORD", "SMTP_PASS", "SMTP_APP_PASSWORD").split()
        )
        self.smtp_from_name = _env("SMTP_FROM_NAME", default="Christ Junior College Recruitment")
        self.smtp_from_email = _env("SMTP_FROM_EMAIL", default=self.smtp_user)
        self.email_dry_run = _flag("STAFF_EMAIL_DRY_RUN")

        # ---- branding / links -----------------------------------------
        self.org_name = _env("STAFF_ORG_NAME", default="Christ Junior College")
        self.support_email = _env("STAFF_SUPPORT_EMAIL", default=self.smtp_user)
        self.notify_email = _env("STAFF_NOTIFY_EMAIL")
        self.public_url = _env("STAFF_PUBLIC_URL", "RENDER_EXTERNAL_URL").rstrip("/")

        # ---- behaviour -----------------------------------------------
        self.code_ttl_days = _int("STAFF_CODE_TTL_DAYS", default=7)
        self.max_recording_mb = _int("STAFF_MAX_RECORDING_MB", default=600)
        self.min_free_disk_mb = _int("STAFF_MIN_FREE_DISK_MB", default=200)
        self.video_bps = _int("STAFF_VIDEO_BPS", default=600_000)
        self.audio_bps = _int("STAFF_AUDIO_BPS", default=64_000)
        self.face_max_attempts = _int("STAFF_FACE_MAX_ATTEMPTS", default=6)
        self.max_resumes = _int("STAFF_MAX_RESUMES", default=3)
        self.trust_proxy = _flag("STAFF_TRUST_PROXY", default=bool(os.environ.get("RENDER")))

    # -- derived helpers ------------------------------------------------
    @property
    def recordings_dir(self):
        return self.data_dir / "recordings"

    @property
    def photos_dir(self):
        return self.data_dir / "photos"

    @property
    def resumes_dir(self):
        return self.data_dir / "resumes"

    @property
    def files_persistent(self):
        return is_persistent(self.data_dir)

    @property
    def database_persistent(self):
        return bool(self.database_url) or self.files_persistent

    def persistence_warnings(self):
        """Plain-language problems that would make recruitment data vanish. Empty list = safe."""
        out = []
        if not self.database_persistent:
            out.append("Positions, applications and interviews are stored on the server's temporary disk and are erased "
                       "whenever it restarts or redeploys. Set DATABASE_URL to a Postgres database (or mount a persistent disk).")
        if not self.files_persistent:
            out.append(f"Recorded videos, face photos and resumes ({self.data_dir}) are on temporary disk and are erased "
                       "on every deploy or restart. Attach a persistent disk mounted at /var/data (or set STAFF_DATA_DIR to one).")
        return out

    @property
    def smtp_configured(self):
        return bool(self.smtp_user and self.smtp_password and self.smtp_host)

    @property
    def ai_configured(self):
        return bool(self.gemini_api_key) or self.ai_mock

    @property
    def admin_configured(self):
        return bool(self.admin_password)


_settings = None


def get_settings():
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings():
    """Re-read the environment (used by tests and by init_staff)."""
    global _settings
    _settings = Settings()
    return _settings
