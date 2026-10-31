"""Exercises GeminiService through the REAL google-genai SDK against a fake Gemini REST server:
request shape (schema, thinking, system instruction), model fallback, retries and TTS/STT parsing."""
import base64
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("google.genai")

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402


def camel(obj):
    """The SDK emits a mix of camelCase and snake_case; Google's JSON mapping accepts both."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            parts = k.split("_")
            out[parts[0] + "".join(x.title() for x in parts[1:])] = camel(v)
        return out
    if isinstance(obj, list):
        return [camel(x) for x in obj]
    return obj


class FakeGemini:
    def __init__(self):
        self.app = Flask("fake")
        self.calls = []
        self.models = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-3.8-flash-lite-tts", "gemini-3.1-pro-preview"]
        self.reject = {}          # model -> (status, message); a "once:" prefix means fail only the first time
        self.replies = []         # queued text replies for non-audio calls
        app = self.app

        @app.get("/v1beta/models")
        def list_models():
            return jsonify({"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                                       for m in self.models]})

        @app.post("/v1beta/models/<path:model_action>")
        def generate(model_action):
            model = model_action.split(":")[0]
            body = camel(request.get_json())
            self.calls.append({"model": model, "body": body})
            if model in self.reject:
                status, msg = self.reject[model]
                if msg.startswith("once:"):
                    self.reject.pop(model)
                    msg = msg[5:]
                return jsonify({"error": {"code": status, "message": msg, "status": "ERR"}}), status
            gc = body.get("generationConfig", {})
            if "AUDIO" in gc.get("responseModalities", []):
                pcm = b"\x01\x00" * 2400
                return jsonify({"candidates": [{"content": {"parts": [{"inlineData": {
                    "mimeType": "audio/L16;rate=24000", "data": base64.b64encode(pcm).decode()}}]}}]})
            text = self.replies.pop(0) if self.replies else "{}"
            return jsonify({"candidates": [{"content": {"parts": [{"text": text}], "role": "model"}, "finishReason": "STOP"}]})


@pytest.fixture()
def fake(monkeypatch):
    fg = FakeGemini()
    server = make_server("127.0.0.1", 0, fg.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("STAFF_AI_MOCK", raising=False)
    for k in ("STAFF_MODEL_TURN", "STAFF_MODEL_EVAL", "STAFF_MODEL_TTS", "STAFF_MODEL_STT"):
        monkeypatch.delenv(k, raising=False)
    from staff.config import reload_settings
    reload_settings()
    from staff.gemini import GeminiService
    yield fg, GeminiService()
    server.shutdown()


TURN = {"answer_assessment": "ok", "answer_quality": 3, "move": "follow_up", "topic_id": "q1", "topic_updates": [],
        "reply": "Could you walk me through that?", "end_interview": False, "integrity_flag": ""}


def test_structured_request_shape_and_parse(fake):
    fg, ai = fake
    from staff.schemas import TurnOut
    fg.replies.append(json.dumps(TURN))
    out = ai.structured("turn", "You are an interviewer.", "Candidate said hi", TurnOut, thinking="low")
    assert out.reply.startswith("Could you walk me")
    call = fg.calls[0]
    assert call["model"] == "gemini-3.8-flash"                       # newest preferred model that exists
    body = call["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == "You are an interviewer."
    gc = body["generationConfig"]
    assert gc["responseMimeType"] == "application/json"
    assert "responseSchema" in gc or "responseJsonSchema" in gc
    assert gc["thinkingConfig"]["thinkingLevel"].upper() == "LOW"


def test_falls_back_when_a_model_is_gone(fake):
    fg, ai = fake
    from staff.schemas import TurnOut
    fg.reject["gemini-3.8-flash"] = (404, "models/gemini-3.8-flash is not found for API version v1beta")
    fg.replies.append(json.dumps(TURN))
    out = ai.structured("turn", "s", "u", TurnOut)
    assert out.move == "follow_up"
    assert [c["model"] for c in fg.calls][:2] == ["gemini-3.8-flash", "gemini-2.5-flash"]
    # the dead model is remembered and not retried next time
    fg.replies.append(json.dumps(TURN))
    ai.structured("turn", "s", "u", TurnOut)
    assert fg.calls[-1]["model"] == "gemini-2.5-flash"
    assert fg.calls[-1]["body"]["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0   # 2.5-style budget


def test_retries_without_thinking_config_when_rejected(fake):
    fg, ai = fake
    from staff.schemas import TurnOut
    fg.reject["gemini-3.8-flash"] = (400, "once:Thinking level is not supported for this model")
    fg.replies.append(json.dumps(TURN))
    ai.structured("turn", "s", "u", TurnOut)
    assert "thinkingConfig" in fg.calls[0]["body"]["generationConfig"]
    assert "thinkingConfig" not in fg.calls[1]["body"]["generationConfig"]
    assert fg.calls[1]["model"] == "gemini-3.8-flash"


def test_repairs_fenced_or_invalid_json(fake):
    fg, ai = fake
    from staff.schemas import TurnOut
    fg.replies += ["```json\n" + json.dumps(TURN) + "\n```"]
    assert ai.structured("turn", "s", "u", TurnOut).reply
    fg.replies += ["not json at all", json.dumps(TURN)]
    assert ai.structured("turn", "s", "u", TurnOut).reply
    assert "valid JSON" in fg.calls[-1]["body"]["contents"][-1]["parts"][-1]["text"]


def test_auth_error_is_reported_not_retried(fake):
    fg, ai = fake
    from staff.gemini import AIError
    from staff.schemas import TurnOut
    for m in fg.models:
        fg.reject[m] = (403, "API key not valid")
    with pytest.raises(AIError) as e:
        ai.structured("turn", "s", "u", TurnOut)
    assert "API key" in str(e.value) and len(fg.calls) == 1


def test_tts_returns_playable_wav(fake):
    fg, ai = fake
    wav = ai.tts("Hello there")
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert fg.calls[0]["model"] == "gemini-3.8-flash-lite-tts"
    voice = fg.calls[0]["body"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert voice == "Kore"
    assert ai.tts("Hello there") == wav and len(fg.calls) == 1        # cached


def test_transcribe_sends_audio_and_filters_silence_markers(fake):
    fg, ai = fake
    fg.replies.append("I fixed the database outage.")
    text = ai.transcribe(b"\x1a\x45\xdf\xa3" + b"0" * 2000, "audio/webm;codecs=opus", "SQL, Linux")
    assert text == "I fixed the database outage."
    parts = fg.calls[0]["body"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["mimeType"] == "audio/webm" and "SQL, Linux" in parts[1]["text"]
    fg.replies.append("[silence]")
    assert ai.transcribe(b"0" * 2000, "audio/webm") == ""


def test_resolver_honours_env_override_and_lists(fake, monkeypatch):
    fg, ai = fake
    monkeypatch.setenv("STAFF_MODEL_TURN", "my-custom-model")
    from staff.config import reload_settings
    reload_settings()
    from staff.gemini import GeminiService
    ai2 = GeminiService()
    assert ai2.candidates("turn")[0] == "my-custom-model"
    assert ai2.candidates("tts")[0] == "gemini-3.8-flash-lite-tts"
    assert ai2.candidates("eval")[0] == "gemini-3.1-pro-preview"
