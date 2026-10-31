"""End-to-end tests of the staff recruitment backend (mock AI, dry-run email)."""
import io
import json
import os
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STAFF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAFF_AI_MOCK", "1")
    monkeypatch.setenv("STAFF_ADMIN_PASSWORD", "correct horse battery")
    monkeypatch.setenv("STAFF_EMAIL_DRY_RUN", "1")
    monkeypatch.setenv("STAFF_PUBLIC_URL", "https://example.test")
    # SQLite by default; set STAFF_TEST_DATABASE_URL to run the same suite against Postgres
    pg = os.environ.get("STAFF_TEST_DATABASE_URL")
    if pg:
        monkeypatch.setenv("DATABASE_URL", pg)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    from flask import Flask
    from staff import init_staff
    from staff.db import TABLES
    from staff.security import limiter
    limiter.reset()
    app = Flask(__name__)
    services = init_staff(app)
    assert services.db.is_pg == bool(pg)
    if pg:  # shared database: start every test from empty tables
        for table in TABLES:
            services.db.run(f"DELETE FROM {table}")
    return app, services


@pytest.fixture()
def client(env):
    return env[0].test_client()


@pytest.fixture()
def admin(client):
    r = client.post("/staff/api/login", json={"password": "correct horse battery"})
    assert r.status_code == 200
    csrf = r.get_json()["csrf"]

    class A:
        def get(self, url, **kw):
            return client.get(url, **kw)

        def post(self, url, json=None, **kw):
            return client.post(url, json=json, headers={"X-CSRF-Token": csrf}, **kw)

        def put(self, url, json=None):
            return client.put(url, json=json, headers={"X-CSRF-Token": csrf})

        def delete(self, url):
            return client.delete(url, headers={"X-CSRF-Token": csrf})
    return A()


def jpeg_bytes(w=640, h=480):
    from PIL import Image
    rnd = random.Random(1)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)) for _ in range(w * h)])
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


POSITION = {
    "title": "Senior Support Engineer", "department": "Customer Success", "description": "Help customers succeed.",
    "required_skills": ["Linux", "SQL"], "seniority": "Senior",
    "eligibility": {"min_experience_years": 3, "min_education": "bachelor", "must_have_skills": ["SQL"]},
    "objectives": "Check troubleshooting depth.",
    "questions": [
        {"text": "Tell me about debugging a hard production issue.", "intent": "Troubleshooting", "required": True, "weight": 5},
        {"text": "How do you explain technical problems to non-technical customers?", "required": True},
        {"text": "Describe a time you disagreed with a teammate.", "required": False},
    ],
    "criteria": [{"label": "Customer empathy", "description": "Understands customer pain", "weight": 4}],
    "interview": {"duration_minutes": 10}, "auto_invite": False,
}


def make_position(admin):
    r = admin.post("/staff/api/positions", json=POSITION)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["position"]


def apply_as_candidate(client, position_id, **over):
    data = {"position_id": position_id, "full_name": "Priya Nair", "email": "priya@example.com",
            "phone": "+91 98765 43210", "experience_years": "5", "education": "master",
            "skills": "Linux, SQL, Python", "current_title": "Support Lead", "cover_note": "I love helping customers."}
    data.update(over)
    data["resume"] = (io.BytesIO(b"Priya Nair\nSupport lead. SQL, Linux, incident response."), "resume.txt")
    return client.post("/api/recruit/apply", data=data, content_type="multipart/form-data")


def run_face_check(client, token):
    h = {"Authorization": f"Bearer {token}"}
    ch = client.post("/api/recruit/face/challenge", headers=h).get_json()
    assert ch["ok"] and ch["tasks"][0] == "center"
    time.sleep(2.6)  # server rejects implausibly fast verifications
    result = {"nonce": ch["nonce"], "detector": "mediapipe",
              "steps": [{"name": t, "passed": True} for t in ch["tasks"]]}
    return client.post("/api/recruit/face/submit", headers=h, content_type="multipart/form-data",
                       data={"photo": (io.BytesIO(jpeg_bytes()), "face.jpg"), "result": json.dumps(result)})


# ------------------------------------------------------------------ auth / security
def test_admin_requires_login_and_csrf(client):
    assert client.get("/staff/api/positions").status_code == 401
    assert client.post("/staff/api/login", json={"password": "nope"}).status_code == 401
    r = client.post("/staff/api/login", json={"password": "correct horse battery"})
    assert r.status_code == 200
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/staff" in cookie
    assert client.get("/staff/api/positions").status_code == 200
    # state-changing call without the CSRF header is refused
    assert client.post("/staff/api/positions", json=POSITION).status_code == 403


def test_login_is_rate_limited(client):
    for _ in range(8):
        client.post("/staff/api/login", json={"password": "bad"})
    assert client.post("/staff/api/login", json={"password": "correct horse battery"}).status_code == 429


def test_pages_have_csp_and_assets_not_traversable(client):
    r = client.get("/staff")
    assert r.status_code == 200 and "Content-Security-Policy" in r.headers
    assert client.get("/staff-static/../config.py").status_code == 404
    assert client.get("/staff-static/%2e%2e/config.py").status_code == 404
    assert client.get("/staff-static/vendor/mediapipe/face_landmarker.task").status_code == 200


# ------------------------------------------------------------------ positions / applications
def test_public_positions_hide_interview_internals(client, admin):
    make_position(admin)
    body = client.get("/api/recruit/positions").get_json()
    p = body["positions"][0]
    assert p["title"] == "Senior Support Engineer"
    flat = json.dumps(p)
    assert "questions" not in flat and "objectives" not in flat and "criteria" not in flat
    assert "Tell me about debugging" not in flat


def test_apply_screens_eligibility_and_dedupes(client, admin):
    pos = make_position(admin)
    r = apply_as_candidate(client, pos["id"])
    assert r.status_code == 201
    app_id = r.get_json()["id"]
    detail = admin.get(f"/staff/api/applications/{app_id}").get_json()["application"]
    assert detail["status"] == "submitted" and detail["eligibility"]["passed"] is True
    assert detail["has_resume"] and "incident response" in detail["resume_excerpt"]
    # a candidate who misses requirements is flagged, not auto-rejected
    low = apply_as_candidate(client, pos["id"], email="low@example.com", experience_years="1", skills="Excel")
    low_id = low.get_json()["id"]
    d2 = admin.get(f"/staff/api/applications/{low_id}").get_json()["application"]
    assert d2["status"] == "needs_review" and not d2["eligibility"]["passed"]
    assert d2["decision"] is None
    # duplicate application within 90 days reuses the original
    again = apply_as_candidate(client, pos["id"])
    assert again.status_code == 201 and again.get_json()["id"] == app_id


def test_apply_validation_and_honeypot(client, admin):
    pos = make_position(admin)
    assert apply_as_candidate(client, pos["id"], email="not-an-email").status_code == 400
    assert apply_as_candidate(client, "pos_missing").status_code == 404
    r = apply_as_candidate(client, pos["id"], website_url="http://spam.test")
    assert r.get_json()["id"] == "ok"
    assert admin.get("/staff/api/applications").get_json()["total"] == 0


def test_resume_rejects_disguised_files(client, admin):
    pos = make_position(admin)
    data = {"position_id": pos["id"], "full_name": "Eve Hacker", "email": "eve@example.com", "experience_years": "4",
            "resume": (io.BytesIO(b"MZ\x90\x00 not a pdf"), "resume.pdf")}
    assert client.post("/api/recruit/apply", data=data, content_type="multipart/form-data").status_code == 400


# ------------------------------------------------------------------ invite + email
def test_invite_generates_unique_code_and_email(env, client, admin):
    pos = make_position(admin)
    app_id = apply_as_candidate(client, pos["id"]).get_json()["id"]
    r = admin.post(f"/staff/api/applications/{app_id}/invite", json={"expires_days": 5, "message": "Looking forward!"})
    assert r.status_code == 200
    body = r.get_json()
    code = body["enrollment"]["code"]
    import re
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", code)
    assert body["link"] == f"https://example.test/join?code={code}"
    env[1].mail.wait_idle()
    emails = admin.get("/staff/api/emails").get_json()["items"]
    kinds = {e["kind"]: e for e in emails}
    assert kinds["invite"]["status"] == "sent" and kinds["invite"]["to_addr"] == "priya@example.com"
    assert "received" in kinds
    # a new code replaces the old one; the old one stops working
    second = admin.post(f"/staff/api/applications/{app_id}/invite", json={}).get_json()["enrollment"]["code"]
    assert second != code
    assert client.post("/api/recruit/enroll/lookup", json={"code": code}).status_code == 410
    assert client.post("/api/recruit/enroll/lookup", json={"code": second.lower().replace("-", " ")}).status_code == 200


def test_email_template_is_escaped_and_complete():
    from staff import mailer
    subject, text, html = mailer.render_invite(
        name="<b>Evil</b> Name", position_title="QA <script>x</script>", code="ABCD-2345", link="https://x.test/join?code=ABCD-2345",
        expires_text="01 January 2030, 10:00 UTC", duration_minutes=15, org_name="Org", support_email="help@x.test",
        custom_message="hi <i>there</i>")
    assert "<script>" not in html and "&lt;script&gt;" in html and "ABCD-2345" in html and "ABCD-2345" in text
    assert "\n" not in subject
    msg = mailer.build_message("a@b.test", "Subject\r\nBcc: evil@x.test", text, html)
    assert "Bcc" not in msg.keys() and "\n" not in msg["Subject"]


def test_lookup_bad_codes(client):
    assert client.post("/api/recruit/enroll/lookup", json={"code": "nonsense"}).status_code == 404
    assert client.post("/api/recruit/enroll/lookup", json={"code": "ABCD-2345"}).status_code == 404
    for _ in range(14):
        client.post("/api/recruit/enroll/lookup", json={"code": "ABCD-2345"})
    assert client.post("/api/recruit/enroll/lookup", json={"code": "ABCD-2345"}).status_code == 429


# ------------------------------------------------------------------ full workflow
def test_full_interview_workflow(env, client, admin):
    services = env[1]
    pos = make_position(admin)
    app_id = apply_as_candidate(client, pos["id"]).get_json()["id"]
    code = admin.post(f"/staff/api/applications/{app_id}/invite", json={}).get_json()["enrollment"]["code"]

    lk = client.post("/api/recruit/enroll/lookup", json={"code": code}).get_json()
    assert lk["stage"] == "face" and lk["applicant"]["first_name"] == "Priya"
    tok = lk["token"]
    h = {"Authorization": f"Bearer {tok}"}

    # no interview before face verification
    assert client.post("/api/recruit/interview/start", headers=h).status_code == 403

    # a forged verification (missing steps) is rejected
    ch = client.post("/api/recruit/face/challenge", headers=h).get_json()
    bad = client.post("/api/recruit/face/submit", headers=h, content_type="multipart/form-data",
                      data={"photo": (io.BytesIO(jpeg_bytes()), "f.jpg"),
                            "result": json.dumps({"nonce": ch["nonce"], "steps": [{"name": "center", "passed": True}]})})
    assert bad.status_code == 422

    ok = run_face_check(client, tok)
    assert ok.status_code == 200, ok.get_json()

    start = client.post("/api/recruit/interview/start", headers=h).get_json()
    assert start["ok"] and start["last_ai_text"] and len(start["turns"]) == 1
    assert start["config"]["tts"] is True

    idx, last = 1, None
    for i in range(12):
        r = client.post("/api/recruit/interview/turn", headers=h, json={
            "text": f"This is my detailed answer number {i} about SQL and incident response.", "turn_idx": idx,
            "meta": {"delay_ms": 1200}}).get_json()
        assert r["ok"] and r["reply"]
        last, idx = r, r["turn_idx"] + 1
        if r["end"]:
            break
    assert last["end"] is True

    # retrying an answered turn is idempotent
    again = client.post("/api/recruit/interview/turn", headers=h, json={"text": "x", "turn_idx": idx - 2}).get_json()
    assert again["replayed"] is True and again["reply"] == last["reply"]

    # chunked recording upload: ordered, resumable, validated
    c0 = client.post("/api/recruit/interview/video/chunk?segment=0&seq=0", headers=dict(h, **{"Content-Type": "video/webm"}),
                     data=b"\x1a\x45\xdf\xa3" + b"A" * 3000)
    assert c0.status_code == 200
    assert client.post("/api/recruit/interview/video/chunk?segment=0&seq=2", headers=dict(h, **{"Content-Type": "video/webm"}),
                       data=b"B" * 100).status_code == 409
    dup = client.post("/api/recruit/interview/video/chunk?segment=0&seq=0", headers=dict(h, **{"Content-Type": "video/webm"}),
                      data=b"Z" * 10).get_json()
    assert dup["duplicate"] is True
    assert client.post("/api/recruit/interview/video/chunk?segment=0&seq=1", headers=dict(h, **{"Content-Type": "video/webm"}),
                       data=b"C" * 2000).status_code == 200
    assert client.post("/api/recruit/interview/video/chunk?segment=0&seq=2", headers=dict(h, **{"Content-Type": "text/html"}),
                       data=b"<script>").status_code == 415

    events = client.post("/api/recruit/interview/events", headers=h, json={"events": [
        {"type": "face_absent", "t": 5000, "duration_ms": 4000}, {"type": "evil", "t": 1}]}).get_json()
    assert events["accepted"] == 1

    fin = client.post("/api/recruit/interview/finish", headers=h, json={"reason": "completed"})
    assert fin.status_code == 200
    # the upload can still be finalised after the interview closes
    assert client.post("/api/recruit/interview/video/complete", headers=h,
                       json={"segment": 0, "chunks": 1, "duration_ms": 9000}).status_code == 409  # 2 chunks, not 1
    assert client.post("/api/recruit/interview/video/complete", headers=h,
                       json={"segment": 0, "chunks": 2, "duration_ms": 9000}).status_code == 200

    # code is single-use
    assert client.post("/api/recruit/enroll/lookup", json={"code": code}).status_code == 409

    # evaluation runs in the background
    deadline = time.time() + 10
    ivs = []
    while time.time() < deadline:
        ivs = admin.get("/staff/api/interviews").get_json()["items"]
        if ivs and ivs[0]["status"] == "evaluated":
            break
        time.sleep(0.2)
    assert ivs and ivs[0]["status"] == "evaluated", ivs
    row = ivs[0]
    assert row["full_name"] == "Priya Nair" and row["position_title"] == "Senior Support Engineer"
    assert row["code"] == code and row["recordings"] == 1 and row["overall_score"] is not None

    detail = admin.get(f"/staff/api/interviews/{row['id']}").get_json()["interview"]
    assert detail["evaluation"]["recommendation"] and detail["turns"][0]["role"] == "ai"
    assert detail["enrollment"]["has_photo"] and detail["recordings"][0]["status"] == "complete"
    assert any(e["type"] == "face_absent" for e in detail["events"])
    assert detail["application"]["status"] == "evaluated"

    rec_id = detail["recordings"][0]["id"]
    full = admin.get(f"/staff/api/recordings/{rec_id}/stream")
    assert full.status_code == 200 and full.headers["Cache-Control"] == "private, no-store"
    full.close()  # release the file handle (Windows cannot delete open files)
    part = admin.get(f"/staff/api/recordings/{rec_id}/stream", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.data == b"\x1a\x45\xdf\xa3"
    part.close()
    photo = admin.get(f"/staff/api/interviews/{row['id']}/face.jpg")
    assert photo.status_code == 200
    photo.close()

    # recordings and photos are NOT reachable without an admin session
    anon = env[0].test_client()
    assert anon.get(f"/staff/api/recordings/{rec_id}/stream").status_code == 401
    assert anon.get(f"/staff/api/interviews/{row['id']}/face.jpg").status_code == 401
    assert anon.get(f"/staff/api/recordings/{rec_id}/download").status_code == 401

    # audit trail records who watched the video
    actions = [a["action"] for a in admin.get("/staff/api/audit").get_json()["items"]]
    assert "view_recording" in actions and "invite" in actions

    # deleting an application removes its recordings from disk
    stats = admin.get("/staff/api/stats").get_json()["stats"]
    assert stats["completed"] == 1 and stats["recordings"] == 1
    path = env[1].storage.recording_path(row["id"], "seg-0.webm")
    assert path.exists()
    assert admin.delete(f"/staff/api/applications/{app_id}").status_code == 200
    assert not path.exists()


def test_resume_after_dropped_connection(env, client, admin):
    pos = make_position(admin)
    app_id = apply_as_candidate(client, pos["id"]).get_json()["id"]
    code = admin.post(f"/staff/api/applications/{app_id}/invite", json={}).get_json()["enrollment"]["code"]
    tok = client.post("/api/recruit/enroll/lookup", json={"code": code}).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert run_face_check(client, tok).status_code == 200
    first = client.post("/api/recruit/interview/start", headers=h).get_json()
    again = client.post("/api/recruit/interview/start", headers=h).get_json()
    assert again["resumed"] is True and again["interview_id"] == first["interview_id"]
    assert again["last_ai_text"] == first["last_ai_text"]


def test_silence_nudges_then_ends(env, client, admin):
    pos = make_position(admin)
    app_id = apply_as_candidate(client, pos["id"]).get_json()["id"]
    code = admin.post(f"/staff/api/applications/{app_id}/invite", json={}).get_json()["enrollment"]["code"]
    tok = client.post("/api/recruit/enroll/lookup", json={"code": code}).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert run_face_check(client, tok).status_code == 200
    client.post("/api/recruit/interview/start", headers=h)
    idx, end = 1, False
    for _ in range(3):
        r = client.post("/api/recruit/interview/turn", headers=h, json={"text": "", "kind": "silence", "turn_idx": idx}).get_json()
        idx, end = r["turn_idx"] + 1, r["end"]
    assert end is True  # three silent turns in a row wrap the session up


def test_tokens_are_scoped_and_tamper_proof(env, client, admin):
    from staff.security import sign_token
    assert client.post("/api/recruit/interview/start").status_code == 401
    assert client.post("/api/recruit/interview/start", headers={"Authorization": "Bearer abc.def"}).status_code == 401
    forged = sign_token({"scope": "admin", "eid": "x"}, 60)
    assert client.post("/api/recruit/interview/start", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    expired = sign_token({"scope": "enroll", "eid": "x"}, -5)
    assert client.post("/api/recruit/interview/start", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_storage_blocks_path_traversal(env):
    from staff.storage import safe_join
    root = env[1].storage.s.recordings_dir
    with pytest.raises(ValueError):
        safe_join(root, "..", "staff.db")
    with pytest.raises(ValueError):
        safe_join(root, "int_abc", "../../secrets.txt")
    assert safe_join(root, "int_abc", "seg-0.webm").name == "seg-0.webm"


def test_gemini_schemas_convert_for_the_sdk():
    """The pydantic models must be accepted by google-genai's schema converter."""
    genai = pytest.importorskip("google.genai")
    from google.genai import _transformers, types
    from staff.schemas import Evaluation, InterviewPlan, TurnOut, FaceCheck, QuestionSuggestions
    client = genai.Client(api_key="test")
    for model in (TurnOut, InterviewPlan, Evaluation, FaceCheck, QuestionSuggestions):
        schema = _transformers.t_schema(client._api_client, model)
        assert schema is not None
        types.GenerateContentConfig(response_mime_type="application/json", response_schema=model)


def test_evaluation_postprocess_is_reproducible_and_drops_invented_quotes():
    from staff.engine import InterviewEngine
    from staff.schemas import DimensionScore, Evaluation, TopicAssessment
    ev = Evaluation(overall_score=99, recommendation="strong_yes", confidence_in_assessment="high", confidence_note="",
                    summary="s", suitability="Suitable", dimensions=[
                        DimensionScore(name="role_fit", score=4, rationale="r"), DimensionScore(name="knowledge", score=4, rationale="r"),
                        DimensionScore(name="communication", score=4, rationale="r"), DimensionScore(name="reasoning", score=4, rationale="r"),
                        DimensionScore(name="confidence", score=4, rationale="r")],
                    topic_assessments=[TopicAssessment(topic_id="q1", question_asked="q", answer_summary="a", score=4,
                                                       evidence=["I fixed the database outage", "I invented this quote"], gaps="")],
                    strengths=[], concerns=[], red_flags=[], suggested_followups=[], integrity_notes="")
    turns = [{"role": "candidate", "body": "Well, I fixed the database outage last year.", "meta": {}}]
    out = InterviewEngine._postprocess(ev, {"config": {}}, turns)
    assert out["overall_score"] == 40 and out["model_overall"] == 99
    assert out["recommendation"] in ("no", "maybe")          # strong_yes vs 40 is reconciled
    assert [d["name"] for d in out["dimensions"]][:2] == ["knowledge", "communication"]
    assert out["topic_assessments"][0]["evidence"] == ["I fixed the database outage"]
