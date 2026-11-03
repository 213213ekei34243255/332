"""Applicant-facing API: apply, code lookup, face check, interview, upload."""
import json
import logging
from functools import wraps

from flask import Blueprint, Response, current_app, g, jsonify, request

from . import languages
from .config import get_settings
from .gemini import AIError
from .security import (clean_str, client_ip, json_error, limiter, rate_limited, verify_token)
from .services import ACTIVE_ENROLLMENT, ServiceError

log = logging.getLogger("staff.public")
bp = Blueprint("staff_public", __name__, url_prefix="/api/recruit")

OPEN_STATES = ("issued", "opened", "verified", "in_progress")


def svc():
    return current_app.extensions["staff"]


def enrollment_required(allowed=OPEN_STATES):
    """Authenticate with the token issued by /enroll/lookup and load the context."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            body = verify_token(token) if token else None
            if not body or body.get("scope") != "enroll":
                return json_error("Your session has expired. Please re-enter your code.", 401)
            s = svc()
            enrollment = s.db.get("enrollments", body["eid"])
            if not enrollment:
                return json_error("Your session has expired. Please re-enter your code.", 401)
            status = s.effective_enrollment_status(enrollment)
            # an interview already under way may finish even if the code passed its expiry
            if status == "expired" and enrollment["status"] == "in_progress":
                status = "in_progress"
            if status not in allowed:
                return json_error({"revoked": "This code is no longer valid.", "expired": "This code has expired.",
                                   "completed": "This interview has already been completed."}.get(status, "Not allowed."), 403)
            g.enrollment = enrollment
            g.application = s.db.get("applications", enrollment["application_id"])
            g.position = s.db.get("positions", g.application["position_id"])
            return fn(*args, **kwargs)
        return wrapper
    return deco


# ------------------------------------------------------------------ enrollment
@bp.get("/positions")
@rate_limited("positions", 120, 600)
def positions():
    s = get_settings()
    items = [svc().public_position(p) for p in svc().list_positions(only_open=True)]
    return jsonify({"ok": True, "org": s.org_name, "positions": items})


@bp.post("/apply")
@rate_limited("apply", 6, 3600)
def apply():
    if request.content_type and request.content_type.startswith("multipart/"):
        data = request.form.to_dict()
        upload = request.files.get("resume")
        resume = (upload.filename, upload.read(5 * 1024 * 1024 + 1)) if upload and upload.filename else None
    else:
        data, resume = (request.get_json(silent=True) or {}), None
    if data.get("website_url") or data.get("company_site"):  # honeypot: bots fill hidden fields
        return jsonify({"ok": True, "id": "ok", "message": "Application received."})
    result = svc().apply(data, resume=resume, ip=client_ip(), base_url=request.url_root)
    return jsonify({"ok": True, "id": result["id"],
                    "message": "Application received. If your profile is a match, you will receive an email with your interview code."}), 201


@bp.post("/enroll/lookup")
def lookup():
    ip = client_ip()
    if not limiter.allow(f"lookup:{ip}", 15, 600):
        return json_error("Too many attempts. Please wait a few minutes and try again.", 429)
    code = (request.get_json(silent=True) or {}).get("code", "")
    try:
        en, app, pos, token = svc().lookup_code(code)
    except ServiceError as exc:
        return json_error(exc.message, exc.status)
    cfg = (pos.get("config") or {}).get("interview") or {}
    s = get_settings()
    return jsonify({
        "ok": True, "token": token, "org": s.org_name,
        "applicant": {"first_name": app["full_name"].split()[0], "full_name": app["full_name"]},
        "position": {"title": pos["title"], "department": pos.get("department")},
        "interview": {"duration_minutes": cfg.get("duration_minutes", 15), "persona": cfg.get("persona_name", "Noah"),
                      "speech_lang": languages.info(cfg.get("language"))["speech"], "allow_typing": cfg.get("allow_typing", True),
                      "language": languages.resolve(cfg.get("language")),
                      "allow_language_choice": bool(cfg.get("allow_language_choice", True)),
                      "languages": languages.public_list(),
                      "tts": bool(s.tts_enabled and svc().ai.configured)},
        "stage": "interview" if en["face_verified_at"] else "face",
        "expires_at": en["expires_at"]})


# ------------------------------------------------------------------ face verification
@bp.post("/face/challenge")
@enrollment_required()
@rate_limited("face-challenge", 30, 600)
def face_challenge():
    return jsonify(dict(svc().face_challenge(g.enrollment), ok=True))


@bp.post("/face/submit")
@enrollment_required()
@rate_limited("face-submit", 20, 600)
def face_submit():
    photo = request.files.get("photo")
    if not photo:
        return json_error("No photo received.")
    try:
        result = json.loads(request.form.get("result", "{}"))
    except ValueError:
        return json_error("Bad verification data.")
    face = svc().face_submit(g.enrollment, photo.read(2_100_000), result)
    return jsonify({"ok": True, "verified": True, "server_check": face["server_check"]["status"]})


# ------------------------------------------------------------------ interview
@bp.post("/interview/start")
@enrollment_required()
@rate_limited("iv-start", 20, 600)
def interview_start():
    requested = clean_str((request.get_json(silent=True) or {}).get("language"), 20)
    try:
        payload = svc().start_interview(g.enrollment, g.application, g.position,
                                        language=requested if languages.is_known(requested) else None)
    except AIError as exc:
        return json_error("The AI interviewer is unavailable right now. Please try again in a minute.", exc.status)
    return jsonify(dict(payload, ok=True))


@bp.post("/interview/turn")
@enrollment_required(("in_progress",))
@rate_limited("iv-turn", 90, 600)
def interview_turn():
    data = request.get_json(silent=True) or {}
    try:
        out = svc().handle_turn(g.enrollment, g.application, g.position, data.get("text", ""),
                                clean_str(data.get("kind"), 12) or "answer", int(data.get("turn_idx", -1)),
                                data.get("meta") or {})
    except AIError:
        return json_error("The interviewer is taking a moment. Retrying…", 503, retry=True)
    except (TypeError, ValueError):
        return json_error("Bad request.")
    return jsonify(dict(out, ok=True))


@bp.post("/interview/transcribe")
@enrollment_required(("in_progress",))
@rate_limited("iv-stt", 120, 600)
def interview_transcribe():
    if (request.content_length or 0) > 6 * 1024 * 1024:
        return json_error("Audio too large.", 413)
    audio = request.get_data(cache=False)
    if len(audio) < 800:
        return jsonify({"ok": True, "text": ""})
    pos = g.position
    terms = ", ".join([pos["title"]] + ((pos.get("config") or {}).get("required_skills") or [])[:12])
    iv = svc()._interview_for(g.enrollment)
    spoken = ((iv or {}).get("state") or {}).get("settings", {}).get("language_code")
    try:
        text = svc().ai.transcribe(audio, request.content_type or "audio/webm", terms, spoken)
    except AIError as exc:
        return json_error("Transcription is unavailable.", exc.status)
    return jsonify({"ok": True, "text": text})


@bp.post("/interview/tts")
@enrollment_required(("in_progress", "verified", "opened", "issued"))
@rate_limited("iv-tts", 200, 600)
def interview_tts():
    s = get_settings()
    if not s.tts_enabled or not svc().ai.configured:
        return json_error("Server speech is disabled.", 501)
    text = clean_str((request.get_json(silent=True) or {}).get("text"), 600, multiline=True)
    if not text:
        return json_error("No text.")
    try:
        wav = svc().ai.tts(text)
    except AIError as exc:
        return json_error("Speech synthesis is unavailable.", exc.status)
    resp = Response(wav, mimetype="audio/wav")
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@bp.post("/interview/events")
@enrollment_required(("in_progress",))
@rate_limited("iv-events", 120, 600)
def interview_events():
    n = svc().add_events(g.enrollment, (request.get_json(silent=True) or {}).get("events"))
    return jsonify({"ok": True, "accepted": n})


@bp.post("/interview/video/chunk")
@enrollment_required(("in_progress", "completed"))
@rate_limited("iv-chunk", 1500, 600)
def video_chunk():
    if (request.content_length or 0) > 13 * 1024 * 1024:
        return json_error("Chunk too large.", 413)
    try:
        segment, seq = int(request.args.get("segment", 0)), int(request.args.get("seq", -1))
    except ValueError:
        return json_error("Bad request.")
    if segment < 0 or segment > 20 or seq < 0:
        return json_error("Bad request.")
    out = svc().save_chunk(g.enrollment, segment, seq, request.get_data(cache=False), request.content_type)
    return jsonify(dict(out, ok=True))


@bp.post("/interview/video/complete")
@enrollment_required(("in_progress", "completed"))
def video_complete():
    d = request.get_json(silent=True) or {}
    try:
        out = svc().complete_recording(g.enrollment, int(d.get("segment", 0)), int(d.get("chunks", -1)),
                                       d.get("duration_ms", 0))
    except (TypeError, ValueError):
        return json_error("Bad request.")
    return jsonify(dict(out, ok=True))


@bp.post("/interview/finish")
@enrollment_required(("in_progress", "completed"))
def interview_finish():
    d = request.get_json(silent=True) or {}
    reason = clean_str(d.get("reason"), 30) or "completed"
    return jsonify(dict(svc().finish_interview(g.enrollment, reason), ok=True))
