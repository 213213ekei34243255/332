"""Staff recruitment system: enrollment -> code + email -> face check -> AI interview
-> recording -> AI evaluation -> admin review (/staff).

Self-contained: `init_staff(app)` is the only integration point with the
existing Flask app, and nothing here touches the Veronica chatbot or Foyer.
"""
import logging
import threading

from flask import jsonify

from .config import reload_settings
from .db import Database
from .gemini import AIError, get_ai, reset_ai
from .mailer import Mailer
from .security import apply_security_headers
from .services import ServiceError, Services
from .storage import Storage

log = logging.getLogger("staff")

PROTECTED_PREFIXES = ("/staff", "/apply", "/join", "/api/recruit")


def init_staff(app):
    settings = reload_settings()
    reset_ai()
    db = Database(settings)
    storage = Storage()
    services = Services(db, storage, Mailer(db), get_ai(), settings)
    app.extensions["staff"] = services

    from .pages import bp as pages_bp
    from .routes_admin import bp as admin_bp
    from .routes_public import bp as public_bp
    app.register_blueprint(admin_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(pages_bp)

    if settings.trust_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @app.errorhandler(ServiceError)
    def _service_error(exc):
        body = {"ok": False, "error": exc.message}
        body.update(exc.extra)
        return jsonify(body), exc.status

    @app.errorhandler(AIError)
    def _ai_error(exc):
        return jsonify({"ok": False, "error": str(exc)}), exc.status

    @app.after_request
    def _staff_headers(resp):
        from flask import request
        if request.path.startswith(PROTECTED_PREFIXES):
            apply_security_headers(resp)
        return resp

    if not settings.admin_configured:
        log.warning("STAFF_ADMIN_PASSWORD / ADMIN_API_KEY not set: /staff sign-in is disabled.")
    if not settings.ai_configured:
        log.warning("GEMINI_API_KEY not set: the AI interviewer is unavailable.")
    if not settings.smtp_configured:
        log.warning("SMTP_USER / SMTP_PASSWORD not set: invitation emails cannot be sent.")
    for warning in settings.persistence_warnings():
        log.warning("DATA WILL BE LOST: %s", warning)
    log.info("Staff system ready (db=%s, data=%s)", "postgres" if db.is_pg else "sqlite", settings.data_dir)
    threading.Thread(target=services.recover, daemon=True, name="staff-recover").start()
    return services
