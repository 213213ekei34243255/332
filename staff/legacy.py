"""Gemini backend for the existing /interview endpoint (used by the Foyer frontend).

Foyer posts {system, messages, max_tokens} and expects {"message": "<text>"}; until now that
was answered by a local Qwen 1.5B model. This keeps the same contract but answers with Gemini,
so the Foyer interview gets the same quality jump as the /staff system.
"""
from .config import get_settings
from .gemini import AIError, get_ai


def available():
    s = get_settings()
    return bool(s.ai_configured and not s.ai_mock and s.gemini_api_key)


def _to_contents(messages):
    """Map OpenAI-style messages to Gemini contents: roles user/model, alternating, user first."""
    merged = []
    for m in messages:
        role = "model" if m.get("role") in ("assistant", "model") else "user"
        text = str(m.get("content", "")).strip()
        if not text:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["text"] += "\n" + text
        else:
            merged.append({"role": role, "text": text})
    if not merged or merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "text": "Begin."})
    return merged


def chat(system, messages, max_tokens=500):
    from google.genai import types
    ai = get_ai()
    contents = [types.Content(role=m["role"], parts=[types.Part.from_text(text=m["text"])])
                for m in _to_contents(messages)]
    wants_json = "json" in (system or "").lower()

    def build(model):
        kwargs = dict(system_instruction=system or None, temperature=0.7,
                      max_output_tokens=max(int(max_tokens or 500), 1200))
        if wants_json:
            kwargs["response_mime_type"] = "application/json"
        cfg = ai._thinking(model, "low")
        if cfg is not None:
            kwargs["thinking_config"] = cfg
        return types.GenerateContentConfig(**kwargs)

    resp, _ = ai._call("turn", build, contents)
    text = (getattr(resp, "text", None) or "").strip()
    if not text:
        raise AIError("Empty response from Gemini.", 502)
    return text
