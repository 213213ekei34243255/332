"""The real app.py with the staff module wired in (Veronica's heavy ML imports are stubbed out)."""
import importlib
import json
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
pytest.importorskip("google.genai")

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from test_gemini_service import FakeGemini  # noqa: E402


@pytest.fixture()
def real_app(monkeypatch, tmp_path):
    fg = FakeGemini()

    @fg.app.post("/local/v1/chat/completions")           # stands in for the llama.cpp server
    def local_model():
        fg.calls.append({"model": "local", "body": request.get_json()})
        return jsonify({"choices": [{"message": {"content": "answer from the local model"}}]})

    server = make_server("127.0.0.1", 0, fg.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_BASE_URL", base)
    monkeypatch.setenv("LOCAL_MODEL_URL", f"{base}/local/v1/chat/completions")
    monkeypatch.setenv("STAFF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STAFF_ADMIN_PASSWORD", "pw-for-tests")
    monkeypatch.setenv("STAFF_EMAIL_DRY_RUN", "1")
    for k in ("DATABASE_URL", "INTERVIEW_API_KEY", "STAFF_AI_MOCK", "RENDER"):
        monkeypatch.delenv(k, raising=False)
    stub = types.ModuleType("Veronica")
    stub.get_veronica_response = lambda **kw: "veronica says hi"
    stub.load_knowledge_base = lambda path: {"questions": []}
    stub.save_knowledge_base = lambda path, data: None
    monkeypatch.setitem(sys.modules, "Veronica", stub)
    sys.modules.pop("app", None)
    appmod = importlib.import_module("app")
    from staff.security import limiter
    limiter.reset()
    yield appmod.app, fg
    server.shutdown()
    sys.modules.pop("app", None)


def test_staff_routes_are_mounted_and_veronica_untouched(real_app):
    app, _ = real_app
    c = app.test_client()
    assert c.get("/staff").status_code == 200
    assert c.get("/apply").status_code == 200
    assert c.get("/api/recruit/positions").status_code == 200
    r = c.post("/predict", json={"message": "hello there", "session_id": "s1"})
    assert r.status_code == 200 and r.get_json()["answer"] == "veronica says hi"


def test_foyer_interview_endpoint_uses_gemini_with_same_contract(real_app):
    app, fg = real_app
    fg.replies.append(json.dumps({"message": "Tell me about a project.", "done": False}))
    r = app.test_client().post("/interview", json={
        "system": "You are Foyer. Respond ONLY with minified JSON {\"message\": string, \"done\": boolean}",
        "messages": [{"role": "user", "content": "[The candidate has just joined.]"}], "max_tokens": 350})
    assert r.status_code == 200
    assert json.loads(r.get_json()["message"])["message"] == "Tell me about a project."
    call = [c for c in fg.calls if c["model"] != "local"][-1]
    assert call["body"]["generationConfig"]["responseMimeType"] == "application/json"
    assert call["body"]["systemInstruction"]["parts"][0]["text"].startswith("You are Foyer")


def test_foyer_conversation_roles_are_mapped_for_gemini(real_app):
    app, fg = real_app
    fg.replies.append("ok")
    app.test_client().post("/interview", json={"system": "s", "messages": [
        {"role": "assistant", "content": "Hi!"}, {"role": "user", "content": "Hello"}, {"role": "user", "content": "again"}]})
    contents = [c for c in fg.calls if c["model"] != "local"][-1]["body"]["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user"]      # leading user turn added, same roles merged


def test_falls_back_to_local_model_when_gemini_is_down(real_app):
    app, fg = real_app
    for m in fg.models:
        fg.reject[m] = (500, "backend error")
    # keep the retry budget tiny so the test is fast
    import staff.gemini as g
    g.GeminiService.BUDGET_SECONDS["turn"] = 3
    r = app.test_client().post("/interview", json={"system": "s", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.get_json()["message"] == "answer from the local model"


def test_interview_backend_can_be_forced_to_local(real_app, monkeypatch):
    app, fg = real_app
    monkeypatch.setenv("INTERVIEW_BACKEND", "local")
    r = app.test_client().post("/interview", json={"system": "s", "messages": [{"role": "user", "content": "hi"}]})
    assert r.get_json()["message"] == "answer from the local model"
    assert not [c for c in fg.calls if c["model"] != "local"]
