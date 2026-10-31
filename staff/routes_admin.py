"""Admin API for the /staff dashboard (cookie session + CSRF header)."""
import logging

from flask import Blueprint, current_app, g, jsonify, make_response, request, send_file

from . import mailer
from .config import get_settings
from .gemini import AIError
from .prompts import suggest_prompt
from .schemas import QuestionSuggestions
from .security import (SESSION_COOKIE, admin_or_media, admin_required, clean_email, clean_list, clean_str, client_ip,
                       constant_time_equal, current_admin, issue_admin_session, is_https, json_error, limiter,
                       media_url_token)

log = logging.getLogger("staff.admin")
bp = Blueprint("staff_admin", __name__, url_prefix="/staff/api")


def svc():
    return current_app.extensions["staff"]


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ session
@bp.post("/login")
def login():
    s = get_settings()
    if not s.admin_configured:
        return json_error("Admin access is not configured. Set STAFF_ADMIN_PASSWORD on the server.", 503)
    ip = client_ip()
    if not limiter.allow(f"login:{ip}", 8, 600):
        return json_error("Too many sign-in attempts. Try again in a few minutes.", 429)
    data = request.get_json(silent=True) or {}
    if not constant_time_equal(data.get("password", ""), s.admin_password):
        svc().db.audit("unknown", "login_failed", "", "", ip)
        return json_error("That password is incorrect.", 401)
    token, csrf = issue_admin_session("admin")
    if data.get("mode") == "token":
        # Foyer's dashboard keeps this in sessionStorage and sends it as `Authorization: Bearer` through its server.
        svc().db.audit("admin", "login", "token", "", ip)
        return jsonify({"ok": True, "token": token, "expires_in": s.session_hours * 3600})
    resp = make_response(jsonify({"ok": True, "csrf": csrf}))
    resp.set_cookie(SESSION_COOKIE, token, max_age=s.session_hours * 3600, httponly=True, secure=is_https(),
                    samesite="Strict", path="/staff")
    svc().db.audit("admin", "login", "", "", ip)
    return resp


@bp.post("/logout")
def logout():
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(SESSION_COOKIE, path="/staff")
    return resp


@bp.get("/me")
def me():
    s = get_settings()
    admin = current_admin()
    return jsonify({"ok": True, "configured": s.admin_configured, "signed_in": bool(admin),
                    "csrf": admin["csrf"] if admin else None, "org": s.org_name})


# ------------------------------------------------------------------ overview
@bp.get("/stats")
@admin_required
def stats():
    return jsonify({"ok": True, "stats": svc().stats()})


@bp.get("/system")
@admin_required
def system():
    s = get_settings()
    ai = svc().ai.status()
    return jsonify({"ok": True, "system": {
        "ai": ai, "ai_mock": s.ai_mock, "tts_enabled": s.tts_enabled,
        "smtp": {"configured": s.smtp_configured, "host": s.smtp_host, "port": s.smtp_port, "user": s.smtp_user,
                 "from_name": s.smtp_from_name, "dry_run": s.email_dry_run},
        "storage": {"dir": str(s.data_dir), "free_mb": svc().storage.free_mb(), "used_mb": svc().storage.used_mb()},
        "database": "postgres" if svc().db.is_pg else "sqlite",
        "persistence": {"database_persistent": s.database_persistent, "files_persistent": s.files_persistent,
                        "warnings": s.persistence_warnings()},
        "public_url": s.public_url or request.url_root.rstrip("/"),
        "org": s.org_name, "session_hours": s.session_hours}})


@bp.post("/system/test-email")
@admin_required
def test_email():
    to = clean_email((request.get_json(silent=True) or {}).get("to"))
    if not to:
        return json_error("Enter a valid email address.")
    subject, text, html = mailer.render_test(org_name=get_settings().org_name)
    ok, err = svc().mail.send_now(to_addr=to, subject=subject, text_body=text, html_body=html, kind="test")
    svc().db.audit("admin", "test_email", to)
    return jsonify({"ok": ok, "error": err}), (200 if ok else 502)


@bp.post("/system/test-ai")
@admin_required
def test_ai():
    try:
        return jsonify(dict(svc().ai.ping(), **{"ok": True}))
    except AIError as exc:
        return json_error(str(exc), exc.status)


@bp.get("/audit")
@admin_required
def audit():
    rows = svc().db.all("SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (_int_arg("limit", 100, 1, 500),))
    return jsonify({"ok": True, "items": rows})


# ------------------------------------------------------------------ positions
@bp.get("/positions")
@admin_required
def list_positions():
    return jsonify({"ok": True, "items": svc().list_positions()})


@bp.post("/positions")
@admin_required
def create_position():
    pos = svc().create_position(request.get_json(silent=True) or {})
    svc().db.audit("admin", "create_position", pos["id"], pos["title"], client_ip())
    return jsonify({"ok": True, "position": pos}), 201


@bp.get("/positions/<pid>")
@admin_required
def get_position(pid):
    pos = svc().db.get("positions", pid)
    return (jsonify({"ok": True, "position": pos}) if pos else json_error("Position not found.", 404))


@bp.put("/positions/<pid>")
@admin_required
def update_position(pid):
    pos = svc().update_position(pid, request.get_json(silent=True) or {})
    svc().db.audit("admin", "update_position", pid, pos["title"], client_ip())
    return jsonify({"ok": True, "position": pos})


@bp.delete("/positions/<pid>")
@admin_required
def delete_position(pid):
    result = svc().delete_position(pid)
    svc().db.audit("admin", "delete_position", pid, str(result), client_ip())
    return jsonify(dict(result, ok=True))


@bp.post("/positions/suggest")
@admin_required
def suggest_questions():
    data = request.get_json(silent=True) or {}
    title = clean_str(data.get("title"), 120)
    if not title:
        return json_error("Add a position title first.")
    system, user = suggest_prompt(title, clean_str(data.get("department"), 80),
                                  clean_str(data.get("description"), 3000, multiline=True),
                                  clean_list(data.get("required_skills")), clean_str(data.get("seniority"), 40))
    try:
        out = svc().ai.structured("eval", system, user, QuestionSuggestions, temperature=0.6, thinking="low", max_tokens=3000)
    except AIError as exc:
        return json_error(str(exc), exc.status)
    return jsonify({"ok": True, "suggestions": out.model_dump()})


# ------------------------------------------------------------------ applications
@bp.get("/applications")
@admin_required
def list_applications():
    return jsonify(dict(svc().list_applications(
        position_id=clean_str(request.args.get("position"), 40), status=clean_str(request.args.get("status"), 30),
        q=clean_str(request.args.get("q"), 80), page=_int_arg("page", 1, 1, 10_000),
        page_size=_int_arg("page_size", 25, 5, 100)), ok=True))


@bp.post("/applications")
@admin_required
def create_application():
    data = dict(request.get_json(silent=True) or {})
    data.setdefault("experience_years", 0)
    if data.get("experience_years") in ("", None):
        data["experience_years"] = 0
    result = svc().apply(data, ip=client_ip(), base_url=request.url_root, actor="admin")
    out = {"ok": True, "id": result["id"]}
    if data.get("invite"):
        inv = svc().invite(result["id"], actor="admin", expires_days=data.get("expires_days"),
                           message=data.get("message", ""), base_url=request.url_root)
        out["enrollment"] = inv["enrollment"]
    svc().db.audit("admin", "create_application", result["id"], "", client_ip())
    return jsonify(out), 201


@bp.get("/applications/<app_id>")
@admin_required
def get_application(app_id):
    return jsonify({"ok": True, "application": svc().application_detail(app_id)})


@bp.patch("/applications/<app_id>")
@admin_required
def patch_application(app_id):
    app = svc().update_application(app_id, request.get_json(silent=True) or {})
    svc().db.audit("admin", "update_application", app_id, str({k: v for k, v in (request.get_json(silent=True) or {}).items() if k == "decision"}), client_ip())
    return jsonify({"ok": True, "application": {"id": app["id"], "decision": app["decision"]}})


@bp.delete("/applications/<app_id>")
@admin_required
def delete_application(app_id):
    svc().delete_application(app_id)
    svc().db.audit("admin", "delete_application", app_id, "", client_ip())
    return jsonify({"ok": True})


@bp.post("/applications/<app_id>/invite")
@admin_required
def invite(app_id):
    data = request.get_json(silent=True) or {}
    result = svc().invite(app_id, actor="admin", expires_days=data.get("expires_days"),
                          message=data.get("message", ""), base_url=request.url_root)
    return jsonify({"ok": True, "enrollment": result["enrollment"], "link": result["link"], "email_id": result["email_id"]})


@bp.post("/applications/<app_id>/resend")
@admin_required
def resend(app_id):
    result = svc().invite(app_id, actor="admin", base_url=request.url_root, resend_only=True)
    return jsonify({"ok": True, "enrollment": result["enrollment"], "link": result["link"], "email_id": result["email_id"]})


@bp.post("/applications/bulk-invite")
@admin_required
def bulk_invite():
    data = request.get_json(silent=True) or {}
    results = []
    for app_id in (data.get("ids") or [])[:100]:
        try:
            r = svc().invite(clean_str(app_id, 40), actor="admin", expires_days=data.get("expires_days"),
                             message=data.get("message", ""), base_url=request.url_root)
            results.append({"id": app_id, "ok": True, "code": r["enrollment"]["code"]})
        except Exception as exc:
            results.append({"id": app_id, "ok": False, "error": getattr(exc, "message", str(exc))})
    return jsonify({"ok": True, "results": results})


@bp.get("/emails")
@admin_required
def list_emails():
    rows = svc().db.all("SELECT * FROM emails ORDER BY created_at DESC LIMIT ?", (_int_arg("limit", 100, 1, 500),))
    return jsonify({"ok": True, "items": rows})


@bp.get("/emails/<email_id>")
@admin_required
def email_status(email_id):
    row = svc().db.get("emails", email_id)
    return jsonify({"ok": True, "email": row}) if row else json_error("Email not found.", 404)


@bp.post("/enrollments/<eid>/revoke")
@admin_required
def revoke(eid):
    svc().revoke_enrollment(eid)
    return jsonify({"ok": True})


@bp.post("/enrollments/<eid>/reset-face")
@admin_required
def reset_face(eid):
    svc().reset_face(eid)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ interviews / videos
@bp.get("/interviews")
@admin_required
def list_interviews():
    return jsonify(dict(svc().list_interviews(
        position_id=clean_str(request.args.get("position"), 40), status=clean_str(request.args.get("status"), 30),
        q=clean_str(request.args.get("q"), 80), page=_int_arg("page", 1, 1, 10_000),
        page_size=_int_arg("page_size", 25, 5, 100)), ok=True))


@bp.get("/interviews/<iid>")
@admin_required
def get_interview(iid):
    return jsonify({"ok": True, "interview": svc().interview_detail(iid)})


@bp.post("/interviews/<iid>/reevaluate")
@admin_required
def reevaluate(iid):
    svc().reevaluate(iid)
    svc().db.audit("admin", "reevaluate", iid, "", client_ip())
    return jsonify({"ok": True})


def _no_store(resp):
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@bp.post("/media-url")
@admin_required
def media_url():
    """Mint a short-lived, single-resource link for <video>/<img>/download (they cannot send auth headers)."""
    data = request.get_json(silent=True) or {}
    kind, rid = clean_str(data.get("type"), 20), clean_str(data.get("id"), 64)
    targets = {
        "recording": ("recordings", "/staff/api/recordings/{}/stream"),
        "download": ("recordings", "/staff/api/recordings/{}/download"),
        "photo": ("interviews", "/staff/api/interviews/{}/face.jpg"),
        "resume": ("applications", "/staff/api/applications/{}/resume"),
    }
    if kind not in targets or not svc().db.get(targets[kind][0], rid):
        return json_error("Not found.", 404)
    return jsonify({"ok": True, "url": f"{targets[kind][1].format(rid)}?st={media_url_token(kind, rid)}"})


@bp.get("/recordings/<rec_id>/stream")
@admin_or_media("recording", "rec_id")
def stream_recording(rec_id):
    rec = svc().db.get("recordings", rec_id)
    if not rec:
        return json_error("Recording not found.", 404)
    try:
        path = svc().storage.recording_path(rec["interview_id"], rec["filename"])
    except ValueError:
        return json_error("Recording not found.", 404)
    if not path.is_file():
        return json_error("The recording file is missing from storage.", 404)
    rng = request.headers.get("Range", "")
    if not rng or rng.startswith("bytes=0-"):  # one audit row per viewing, not per range request
        svc().db.audit("admin", "view_recording", rec_id, rec["interview_id"], client_ip())
    return _no_store(send_file(path, mimetype=rec["mime"], conditional=True, max_age=0))


@bp.get("/recordings/<rec_id>/download")
@admin_or_media("download", "rec_id")
def download_recording(rec_id):
    rec = svc().db.get("recordings", rec_id)
    if not rec:
        return json_error("Recording not found.", 404)
    iv = svc().db.get("interviews", rec["interview_id"])
    app = svc().db.get("applications", iv["application_id"]) if iv else None
    name = f"{(app or {}).get('full_name', 'interview').replace(' ', '_')}-seg{rec['segment']}.{rec['filename'].rsplit('.', 1)[-1]}"
    try:
        path = svc().storage.recording_path(rec["interview_id"], rec["filename"])
    except ValueError:
        return json_error("Recording not found.", 404)
    if not path.is_file():
        return json_error("The recording file is missing from storage.", 404)
    svc().db.audit("admin", "download_recording", rec_id, rec["interview_id"], client_ip())
    return _no_store(send_file(path, mimetype=rec["mime"], as_attachment=True, download_name=name, max_age=0))


@bp.delete("/recordings/<rec_id>")
@admin_required
def delete_recording(rec_id):
    svc().delete_recording(rec_id, "admin")
    return jsonify({"ok": True})


@bp.get("/interviews/<iid>/face.jpg")
@admin_or_media("photo", "iid")
def face_photo(iid):
    iv = svc().db.get("interviews", iid)
    if not iv:
        return json_error("Interview not found.", 404)
    path = svc().storage.photo_path(iv["enrollment_id"])
    if not path.is_file():
        return json_error("No verification photo on file.", 404)
    svc().db.audit("admin", "view_face_photo", iid, "", client_ip())
    return _no_store(send_file(path, mimetype="image/jpeg", max_age=0))


@bp.get("/applications/<app_id>/resume")
@admin_or_media("resume", "app_id")
def resume(app_id):
    app = svc().db.get("applications", app_id)
    if not app or not app.get("resume_file"):
        return json_error("No resume on file.", 404)
    from .storage import safe_join
    try:
        path = safe_join(get_settings().resumes_dir, app["resume_file"])
    except ValueError:
        return json_error("No resume on file.", 404)
    if not path.is_file():
        return json_error("No resume on file.", 404)
    svc().db.audit("admin", "view_resume", app_id, "", client_ip())
    return _no_store(send_file(path, mimetype=app.get("resume_mime") or "application/octet-stream", as_attachment=True,
                               download_name=f"{app['full_name'].replace(' ', '_')}-resume.{app['resume_file'].rsplit('.', 1)[-1]}",
                               max_age=0))
