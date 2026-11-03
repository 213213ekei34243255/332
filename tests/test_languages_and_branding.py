"""Six interview languages, Christ Junior College branding, and 'the underlying vendor is never named' (Rexy)."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_staff_flow import (POSITION, admin, apply_as_candidate, client, env, make_position, run_face_check)  # noqa: F401,E402

WEB = Path(__file__).resolve().parent.parent / "staff" / "web"
CODES = ["en", "ml", "kn", "ta", "te", "hi"]
SCRIPT = {"ml": r"[ഀ-ൿ]", "kn": r"[ಀ-೿]", "ta": r"[஀-௿]", "te": r"[ఀ-౿]", "hi": r"[ऀ-ॿ]"}


def position_with(admin, **interview):
    body = dict(POSITION, interview=dict({"duration_minutes": 10}, **interview))
    r = admin.post("/staff/api/positions", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["position"]


def enroll(client, admin, position_id, email="priya@example.com"):
    app_id = apply_as_candidate(client, position_id, email=email).get_json()["id"]
    code = admin.post(f"/staff/api/applications/{app_id}/invite", json={}).get_json()["enrollment"]["code"]
    look = client.post("/api/recruit/enroll/lookup", json={"code": code}).get_json()
    assert run_face_check(client, look["token"]).status_code == 200
    return app_id, look["token"], look


# ------------------------------------------------------------------ language table
def test_every_language_has_its_own_script_and_complete_fixed_lines():
    from staff import languages
    assert list(languages.LANGUAGES) == CODES
    for code in CODES:
        info = languages.info(code)
        for key in ("sample", "opening", "closing", "fallback"):
            line = languages.line(code, key, name="Priya", persona="Noah")
            assert line and "{" not in line, (code, key)
            if code != "en":
                assert re.search(SCRIPT[code], line), (code, key, line)
        assert "Priya" in languages.line(code, "closing", name="Priya")
        assert info["speech"].startswith(code)


def test_language_names_codes_and_locales_all_resolve():
    from staff import languages
    assert languages.resolve("Malayalam") == "ml" and languages.resolve("ml-IN") == "ml" and languages.resolve("മലയാളം") == "ml"
    assert languages.resolve("KANNADA") == "kn" and languages.resolve("ta_IN") == "ta" and languages.resolve("తెలుగు") == "te"
    assert languages.resolve("hi") == "hi" and languages.resolve("English") == "en"
    assert languages.resolve("Klingon") == "en" and languages.resolve("") == "en" and languages.resolve(None) == "en"
    assert not languages.is_known("Klingon") and languages.is_known("Tamil")
    assert [x["code"] for x in languages.public_list()] == CODES
    # candidate's pick wins only when the position allows it
    assert languages.choose("English", "ml", True) == "ml"
    assert languages.choose("Hindi", "ml", False) == "hi"
    assert languages.choose("Hindi", "bogus", True) == "hi" and languages.choose("Hindi", None, True) == "hi"


# ------------------------------------------------------------------ position settings
def test_position_language_is_normalised_and_defaults_allow_choice(admin):
    pos = position_with(admin, language="Malayalam", speech_lang="en-US")        # a stale locale must not win
    iv = pos["config"]["interview"]
    assert iv["language"] == "Malayalam" and iv["speech_lang"] == "ml-IN" and iv["allow_language_choice"] is True
    assert position_with(admin, language="Klingon")["config"]["interview"]["language"] == "English"
    assert position_with(admin, language="Tamil", allow_language_choice=False)["config"]["interview"]["allow_language_choice"] is False


def test_staff_category_is_validated_and_shown_to_applicants(client, admin):
    ok = admin.post("/staff/api/positions", json=dict(POSITION, staff_type="Teaching staff")).get_json()["position"]
    bad = admin.post("/staff/api/positions", json=dict(POSITION, title="Other", staff_type="Wizard")).get_json()["position"]
    assert ok["config"]["staff_type"] == "Teaching staff" and bad["config"]["staff_type"] == ""
    shown = {p["id"]: p for p in client.get("/api/recruit/positions").get_json()["positions"]}
    assert shown[ok["id"]]["staff_type"] == "Teaching staff"


# ------------------------------------------------------------------ the interview itself
def test_lookup_lists_all_six_languages_with_the_position_default(client, admin):
    pos = position_with(admin, language="Kannada")
    _, _, look = enroll(client, admin, pos["id"])
    iv = look["interview"]
    assert [l["code"] for l in iv["languages"]] == CODES
    assert iv["language"] == "kn" and iv["speech_lang"] == "kn-IN" and iv["allow_language_choice"] is True
    assert all("{name}" in l["sample"] for l in iv["languages"])


def test_candidate_chosen_language_drives_the_whole_interview(env, client, admin):
    pos = position_with(admin, language="English")
    app_id, token, _ = enroll(client, admin, pos["id"])
    h = {"Authorization": f"Bearer {token}"}
    start = client.post("/api/recruit/interview/start", headers=h, json={"language": "ml"}).get_json()
    assert start["ok"] and start["config"]["language_code"] == "ml" and start["config"]["speech_lang"] == "ml-IN"
    assert start["config"]["language"] == "Malayalam"
    assert re.search(SCRIPT["ml"], start["last_ai_text"])                 # the opening line is already in Malayalam

    iv = env[1].db.one("SELECT * FROM interviews WHERE application_id = ?", (app_id,), "interviews")
    assert iv["state"]["settings"]["language_code"] == "ml" and iv["state"]["settings"]["speech_lang"] == "ml-IN"

    turn = client.post("/api/recruit/interview/turn", headers=h, json={"text": "ഞാൻ അഞ്ച് വർഷമായി പഠിപ്പിക്കുന്നു", "kind": "answer", "turn_idx": 1}).get_json()
    assert turn["ok"] and re.search(SCRIPT["ml"], turn["reply"])            # the interviewer keeps answering in Malayalam
    stored = admin.get(f"/staff/api/interviews/{iv['id']}").get_json()["interview"]
    assert stored["language"] == "Malayalam"
    assert any("ഞാൻ" in t["text"] for t in stored["turns"])                # Indic text survives storage and the API

    # a reload resumes in the language the interview started in, whatever the page asks for now
    again = client.post("/api/recruit/interview/start", headers=h, json={"language": "hi"}).get_json()
    assert again["resumed"] and again["config"]["language_code"] == "ml"

    # speech-to-text is told which language to expect (the mock answers in that language)
    heard = client.post("/api/recruit/interview/transcribe", headers=dict(h, **{"Content-Type": "audio/webm"}), data=b"x" * 2000).get_json()
    assert re.search(SCRIPT["ml"], heard["text"])


def test_position_can_pin_the_language(client, admin):
    pos = position_with(admin, language="Hindi", allow_language_choice=False)
    _, token, look = enroll(client, admin, pos["id"], email="fixed@example.com")
    assert look["interview"]["allow_language_choice"] is False and look["interview"]["language"] == "hi"
    start = client.post("/api/recruit/interview/start", headers={"Authorization": f"Bearer {token}"}, json={"language": "ta"}).get_json()
    assert start["config"]["language_code"] == "hi" and re.search(SCRIPT["hi"], start["last_ai_text"])


def test_unknown_language_request_falls_back_to_the_position_default(client, admin):
    pos = position_with(admin, language="Telugu")
    _, token, _ = enroll(client, admin, pos["id"], email="odd@example.com")
    start = client.post("/api/recruit/interview/start", headers={"Authorization": f"Bearer {token}"}, json={"language": "klingon"}).get_json()
    assert start["config"]["language_code"] == "te"


def test_speech_text_in_every_language_passes_through_tts_and_input_cleaning(client, admin):
    from staff import languages
    pos = position_with(admin, language="English")
    _, token, _ = enroll(client, admin, pos["id"], email="tts@example.com")
    h = {"Authorization": f"Bearer {token}"}
    for code in CODES:
        r = client.post("/api/recruit/interview/tts", headers=h, json={"text": languages.line(code, "closing", name="Priya")})
        assert r.status_code == 200 and r.data[:4] == b"RIFF", code


# ------------------------------------------------------------------ prompts
def test_prompts_demand_native_script_replies_and_english_assessments():
    from staff import prompts
    pos = {"title": "Physics Teacher", "config": {"staff_type": "Teaching staff", "interview": {"language": "English"}}}
    app = {"full_name": "Priya Nair", "profile": {}}
    settings = prompts.interview_settings(pos, "ml")
    assert settings["language_code"] == "ml" and settings["speech_lang"] == "ml-IN" and settings["language"] == "Malayalam"
    system = prompts.turn_system(pos, app, {"plan": {}}, settings, "Christ Junior College")
    assert "Malayalam (മലയാളം)" in system and "native script" in system and "Christ Junior College is a junior college" in system
    assert "Staff category: Teaching staff" in system
    english = prompts.turn_system(pos, app, {"plan": {}}, prompts.interview_settings(pos, "en"), "Christ Junior College")
    assert "Speak in clear, natural English" in english and "native script" not in english
    state = {"settings": settings, "topics": [], "plan": {}}
    _, ev_user = prompts.eval_prompt(pos, app, state, [], "none", "Christ Junior College")
    assert "write every field in English" in ev_user and "interview language: Malayalam" in ev_user
    plan_system, _ = prompts.plan_prompt(pos, app, settings, "Christ Junior College")
    assert "spoken in Malayalam" in plan_system


def test_evidence_quotes_in_indic_scripts_are_checked_verbatim():
    from staff.engine import InterviewEngine
    from staff.schemas import Evaluation
    said = "ഞാൻ കുട്ടികളെ ഗണിതം പഠിപ്പിക്കുന്നത് കളികളിലൂടെയാണ്, അതിനാൽ അവർക്ക് ഇഷ്ടമാണ്."
    ev = Evaluation.model_validate({
        "overall_score": 70, "recommendation": "yes", "confidence_in_assessment": "medium", "confidence_note": "", "summary": "s",
        "suitability": "Suitable", "dimensions": [{"name": "knowledge", "score": 7, "rationale": "r"}],
        "topic_assessments": [{"topic_id": "q1", "question_asked": "q", "answer_summary": "a", "score": 7, "gaps": "",
                               "evidence": ["കുട്ടികളെ ഗണിതം പഠിപ്പിക്കുന്നത് കളികളിലൂടെയാണ്", "ഞാൻ ഒരിക്കലും പരീക്ഷ നടത്താറില്ല"]}],
        "strengths": [], "concerns": [], "red_flags": [], "suggested_followups": [], "integrity_notes": ""})
    out = InterviewEngine._postprocess(ev, {"config": {}}, [{"role": "candidate", "body": said}])
    assert out["topic_assessments"][0]["evidence"] == ["കുട്ടികളെ ഗണിതം പഠിപ്പിക്കുന്നത് കളികളിലൂടെയാണ്"]


# ------------------------------------------------------------------ Christ Junior College branding + logos
def test_default_branding_is_christ_junior_college(client, admin):
    from staff.config import get_settings
    s = get_settings()
    assert s.org_name == "Christ Junior College" and s.smtp_from_name == "Christ Junior College Recruitment"
    assert client.get("/api/recruit/positions").get_json()["org"] == "Christ Junior College"
    assert admin.get("/staff/api/me").get_json()["org"] == "Christ Junior College"


def test_every_page_carries_both_logos_and_the_favicon(client):
    for path in ("/staff", "/apply", "/join"):
        r = client.get(path)
        assert r.status_code == 200, path
        html = r.get_data(as_text=True)
        assert "/staff-static/img/cjc-logo.png" in html and "/staff-static/img/cogniai-logo.png" in html, path
        assert "/staff-static/img/favicon.png" in html and "Christ Junior College" in html, path
        assert "style=" not in html, f"{path}: inline styles would be blocked by the CSP"
    for name in ("cjc-logo.png", "cogniai-logo.png", "favicon.png"):
        img = client.get(f"/staff-static/img/{name}")
        assert img.status_code == 200 and img.mimetype == "image/png" and img.data[:8] == b"\x89PNG\r\n\x1a\n", name


def test_no_page_asset_or_api_response_names_the_old_company_or_the_ai_vendor(client, admin):
    banned = re.compile(r"gemini|abraham", re.I)
    for path in sorted(WEB.glob("*")):
        if path.suffix in (".html", ".css", ".js"):
            assert not banned.search(path.read_text(encoding="utf-8")), f"{path.name} mentions a banned name"
    for url in ("/api/recruit/positions", "/staff/api/system", "/staff/api/me", "/staff/api/stats"):
        assert not banned.search(admin.get(url).get_data(as_text=True)), url
    out = admin.post("/staff/api/system/test-ai", json={})
    assert out.status_code == 200 and not banned.search(out.get_data(as_text=True))


def test_vendor_names_are_scrubbed_from_errors_and_model_labels():
    from staff.gemini import AIError, display_model, scrub
    assert scrub("models/gemini-3.8-flash is not found for API version v1beta.") == "The Rexy model is not found for API version v1beta."
    assert scrub("Empty response from Gemini.") == "Empty response from the Rexy model."
    assert str(AIError("Gemini rejected the key")) == "The Rexy model rejected the key"
    assert [display_model(m) for m in ("gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-3.8-flash-lite-tts", "gemini-2.5-flash-lite")] == [
        "Rexy Fast", "Rexy Pro", "Rexy Voice", "Rexy Lite"]


def test_the_ai_key_is_accepted_under_the_rexy_name_and_the_old_one(monkeypatch):
    from staff.config import Settings
    monkeypatch.delenv("REXY_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "legacy-key")
    assert Settings().gemini_api_key == "legacy-key"
    monkeypatch.setenv("REXY_API_KEY", "rexy-key")
    assert Settings().gemini_api_key == "rexy-key"
