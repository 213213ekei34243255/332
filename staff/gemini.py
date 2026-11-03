"""Gemini access layer (google-genai SDK).

Why this looks defensive: Google retires and renames models quickly, and which
models a given API key may use varies by account. So models are *resolved at
runtime* -- an env override wins, then the first preferred model that
`models.list()` says exists, with automatic fallback to the next candidate on
"not found" / quota / transient errors. Nothing here hardcodes a single model.
"""
import io
import json
import logging
import re
import struct
import threading
import time
from collections import OrderedDict

from .config import get_settings

log = logging.getLogger("staff.gemini")

# Newest first. Resolved against models.list(), so unavailable ids are skipped.
PREFERENCES = {
    "turn": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview",
             "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "eval": ["gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.6-flash",
             "gemini-3-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash"],
    "stt": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
            "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "vision": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
               "gemini-2.5-flash", "gemini-2.5-flash-lite"],
    "tts": ["gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview",
            "gemini-2.5-flash-preview-tts"],
}
_EXCLUDED = re.compile(r"tts|image|live|embed|robotics|computer|native|audio|transcribe|omni|banana|veo|imagen")


_VENDOR_ID = re.compile(r"\b(the\s+)?(?:models/)?gemini(?:[\w\-]|\.(?=\d))*", re.I)


def scrub(text):
    """Staff-facing text never names the underlying vendor: it is the 'Rexy model' everywhere a person can see it."""
    text = str(text or "")
    # "the Gemini API" -> "the Rexy model API"; a bare "Gemini" or model id -> "the Rexy model"
    out = _VENDOR_ID.sub(lambda m: ("The" if (m.group(1) or "")[:1] == "T" else "the") + " Rexy model", text)
    if re.match(r"\s*(?:models/)?gemini", text, re.I):                                 # keep sentence-initial capital
        out = out[:1].upper() + out[1:]
    return out


def display_model(name):
    """Friendly label for a resolved model id, e.g. 'Rexy Fast' / 'Rexy Pro' / 'Rexy Voice'."""
    n = (name or "").lower()
    if not n or n == "mock":
        return "Rexy" if not n else "Rexy (test mode)"
    if "tts" in n:
        return "Rexy Voice"
    if "pro" in n:
        return "Rexy Pro"
    if "lite" in n:
        return "Rexy Lite"
    if "flash" in n:
        return "Rexy Fast"
    return "Rexy"


class AIError(Exception):
    """Raised when the Rexy model cannot produce a usable answer."""

    def __init__(self, message, status=503):
        super().__init__(scrub(message))
        self.status = status


def _version_key(name):
    nums = re.findall(r"\d+(?:\.\d+)?", name)
    try:
        return float(nums[0]) if nums else 0.0
    except ValueError:
        return 0.0


def pcm_to_wav(pcm, sample_rate=24000, channels=1, bits=16):
    byte_rate = sample_rate * channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate,
                                    channels * bits // 8, bits)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def extract_json(text):
    """Parse a JSON object out of model text, tolerating code fences / chatter."""
    if not text:
        raise ValueError("empty response")
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(cleaned[start:end + 1])


class GeminiService:
    def __init__(self):
        self.s = get_settings()
        self._client = None
        self._client_lock = threading.Lock()
        self._available = None
        self._available_at = 0.0
        self._bad = set()            # (role, model) pairs that returned "not found"
        self._no_thinking = set()    # models that rejected a thinking config
        self._no_schema = set()      # models that rejected response_schema
        self._tts_cache = OrderedDict()
        self._tts_lock = threading.Lock()
        self.last_error = ""

    # ---- client / models --------------------------------------------------
    @property
    def configured(self):
        return bool(self.s.gemini_api_key)

    @property
    def client(self):
        with self._client_lock:
            if self._client is None:
                from google import genai
                from google.genai import types
                opts = {"timeout": 60_000}
                if self.s.gemini_base_url:
                    opts["base_url"] = self.s.gemini_base_url
                self._client = genai.Client(api_key=self.s.gemini_api_key,
                                            http_options=types.HttpOptions(**opts))
            return self._client

    def _list_models(self):
        if self._available is not None and time.time() - self._available_at < 1800:
            return self._available
        try:
            names = set()
            for m in self.client.models.list():
                names.add(str(m.name).replace("models/", ""))
            self._available = names
        except Exception as exc:
            log.warning("models.list failed (%s); falling back to preference order", type(exc).__name__)
            self._available = set()
        self._available_at = time.time()
        return self._available

    def candidates(self, role):
        override = {"turn": self.s.model_turn, "eval": self.s.model_eval, "tts": self.s.model_tts,
                    "stt": self.s.model_stt}.get(role, "")
        available = self._list_models()
        out = [override] if override else []
        prefs = PREFERENCES.get(role, PREFERENCES["turn"])
        if available:
            out += [m for m in prefs if m in available]
            if role != "tts":
                extra = sorted((n for n in available if n.startswith("gemini-") and "flash" in n
                                and not _EXCLUDED.search(n) and n not in prefs),
                               key=_version_key, reverse=True)
                out += extra[:2]
        else:
            out += prefs
        seen, final = set(), []
        for m in out:
            if m and m not in seen and (role, m) not in self._bad:
                seen.add(m)
                final.append(m)
        if not final:  # everything marked bad: clear and try the preferences again
            self._bad = {b for b in self._bad if b[0] != role}
            final = list(prefs)
        return final

    def resolved_models(self):
        """Used by the admin 'system' panel."""
        return {role: (self.candidates(role)[:1] or [""])[0] for role in PREFERENCES}

    def public_models(self):
        """The same map with vendor model ids replaced by Rexy labels (safe to show in the dashboard)."""
        return {role: display_model(m) for role, m in self.resolved_models().items()}

    # ---- config helpers ---------------------------------------------------
    def _thinking(self, model, level):
        from google.genai import types
        if model in self._no_thinking or level is None:
            return None
        if model.startswith("gemini-3"):
            return types.ThinkingConfig(thinking_level=level)
        if model.startswith("gemini-2.5"):
            if "pro" in model:
                return types.ThinkingConfig(thinking_budget=128 if level in ("minimal", "low") else -1)
            return types.ThinkingConfig(thinking_budget=0 if level in ("minimal", "low") else -1)
        return None

    @staticmethod
    def _retry_delay(exc, attempt):
        m = re.search(r"retry in ([\d.]+)s", str(exc), re.I) or re.search(r"retryDelay\W+(\d+)s", str(exc))
        if m:
            return min(float(m.group(1)) + 0.5, 20)
        return min(1.5 * (2 ** attempt), 12)

    BUDGET_SECONDS = {"turn": 50, "stt": 40, "tts": 30, "vision": 40, "eval": 240}

    def _call(self, role, build_config, contents, *, retries=2):
        """generate_content with model fallback + retry/backoff. Returns (response, model).

        A per-role time budget keeps a live interview turn from hanging behind many retries."""
        from google.genai import errors
        if not self.configured:
            raise AIError("The Rexy model is not configured (REXY_API_KEY is missing).")
        last = None
        deadline = time.time() + self.BUDGET_SECONDS.get(role, 50)
        for model in self.candidates(role):
            attempt = 0
            while attempt <= retries:
                if time.time() > deadline:
                    self.last_error = "The Rexy model took too long to respond."
                    raise AIError(self.last_error, 503)
                try:
                    resp = self.client.models.generate_content(
                        model=model, contents=contents, config=build_config(model))
                    self.last_error = ""
                    return resp, model
                except errors.APIError as exc:
                    last = exc
                    code = getattr(exc, "code", 0) or 0
                    msg = str(exc).lower()
                    if code in (401, 403):
                        self.last_error = "The Rexy model rejected the API key or is not permitted for it."
                        raise AIError(self.last_error, 503)
                    if code == 404 or "not found" in msg or "no longer available" in msg:
                        self._bad.add((role, model))
                        log.warning("model %s unavailable for %s; trying next", model, role)
                        break
                    if code == 400 and "thinking" in msg and model not in self._no_thinking:
                        self._no_thinking.add(model)
                        continue
                    if code == 400 and ("schema" in msg or "response_mime" in msg) and model not in self._no_schema:
                        self._no_schema.add(model)
                        continue
                    if code == 429 or code >= 500:
                        if attempt < retries:
                            time.sleep(self._retry_delay(exc, attempt))
                            attempt += 1
                            continue
                        break  # next model
                    self.last_error = f"The Rexy model rejected the request ({code})."
                    raise AIError(self.last_error, 502)
                except Exception as exc:  # network, timeout, SDK problems
                    last = exc
                    if attempt < retries:
                        time.sleep(self._retry_delay(exc, attempt))
                        attempt += 1
                        continue
                    break
        self.last_error = f"The Rexy model is unavailable right now ({type(last).__name__ if last else 'no model'})."
        raise AIError(self.last_error, 503)

    # ---- structured JSON --------------------------------------------------
    def structured(self, role, system, user, schema, *, temperature=0.4, thinking="low",
                   max_tokens=2048, parts=None, hint=None):
        from google.genai import types
        contents = []
        if parts:
            contents.extend(parts)
        contents.append(user)

        def build(model):
            kwargs = dict(system_instruction=system, temperature=temperature,
                          max_output_tokens=max_tokens, response_mime_type="application/json")
            if model not in self._no_schema:
                kwargs["response_schema"] = schema
            thinking_cfg = self._thinking(model, thinking)
            if thinking_cfg is not None:
                kwargs["thinking_config"] = thinking_cfg
            return types.GenerateContentConfig(**kwargs)

        last_text = ""
        for attempt in range(2):
            resp, model = self._call(role, build, contents)
            last_text = (getattr(resp, "text", None) or "").strip()
            try:
                return schema.model_validate(extract_json(last_text))
            except Exception as exc:
                log.warning("structured parse failed (%s) on %s, attempt %s", type(exc).__name__, model, attempt + 1)
                contents = contents[:-1] + [user + "\n\nYour previous reply was not valid JSON for the required "
                                                   "schema. Reply again with ONLY the JSON object."]
        raise AIError("The AI returned an unreadable response.", 502)

    # ---- speech to text ---------------------------------------------------
    def transcribe(self, audio, mime, hint_terms="", language=None):
        from google.genai import types
        from . import languages
        mime = (mime or "audio/webm").split(";")[0].strip().lower()
        if mime not in ("audio/webm", "audio/wav", "audio/x-wav", "audio/mp4", "audio/mpeg",
                        "audio/mp3", "audio/ogg", "audio/aac", "audio/flac", "audio/m4a"):
            mime = "audio/webm"
        lang = languages.info(language) if language else None
        lang_hint = ""
        if lang and lang["code"] != "en":
            lang_hint = (f" The speaker is most likely speaking {lang['name']} ({lang['native']}), possibly mixed with "
                         f"English words. Write {lang['name']} in its own script, and keep English words in English letters.")
        prompt = ("Transcribe the speech in this audio exactly as spoken, in the original language. "
                  "Output only the transcript text, with natural punctuation. Do not summarise, translate, "
                  "or add commentary. If there is no intelligible speech, output nothing." + lang_hint
                  + (f" Domain vocabulary that may appear: {hint_terms}." if hint_terms else ""))

        def build(model):
            kwargs = dict(temperature=0.0, max_output_tokens=1024)
            cfg = self._thinking(model, "minimal" if model.startswith("gemini-3.6") or "lite" in model else "low")
            if cfg is not None:
                kwargs["thinking_config"] = cfg
            return types.GenerateContentConfig(**kwargs)

        resp, _ = self._call("stt", build, [types.Part.from_bytes(data=audio, mime_type=mime), prompt])
        text = (getattr(resp, "text", None) or "").strip()
        if re.fullmatch(r"[\[\(]?\s*(silence|no speech|inaudible|unintelligible)[^\]\)]*[\]\)]?\.?", text, re.I):
            return ""
        return text

    # ---- text to speech ---------------------------------------------------
    def tts(self, text, voice=None):
        from google.genai import types
        voice = voice or self.s.tts_voice
        key = (voice, text)
        with self._tts_lock:
            if key in self._tts_cache:
                self._tts_cache.move_to_end(key)
                return self._tts_cache[key]

        def build(model):
            return types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))),
            )

        resp, _ = self._call("tts", build, text, retries=1)
        data = None
        try:
            for part in resp.candidates[0].content.parts:
                if getattr(part, "inline_data", None) and part.inline_data.data:
                    data = part.inline_data.data
                    break
        except (AttributeError, IndexError, TypeError):
            data = None
        if not data:
            raise AIError("Speech synthesis returned no audio.", 502)
        wav = data if data[:4] == b"RIFF" else pcm_to_wav(data)
        with self._tts_lock:
            self._tts_cache[key] = wav
            while len(self._tts_cache) > 48:
                self._tts_cache.popitem(last=False)
        return wav

    # ---- vision ------------------------------------------------------------
    def check_face(self, jpeg_bytes):
        from google.genai import types
        from .schemas import FaceCheck
        system = ("You verify webcam selfies for an interview identity check. Judge only what is visible. "
                  "Be strict about the photo being a live person looking at the camera, not a picture of a "
                  "screen, a printed photo, a mask, or a cartoon.")
        user = ("Inspect this selfie. Count the human faces, decide whether a single live person is clearly "
                "visible and facing the camera, and whether it looks like a photo of a screen or print.")
        return self.structured("vision", system, user, FaceCheck, temperature=0.0, thinking="low",
                               max_tokens=400, parts=[types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")])

    def ping(self):
        """Cheap end-to-end check used by the admin 'Test AI' button."""
        from .schemas import Ping
        started = time.time()
        self.structured("turn", "Reply with the JSON object {\"ok\": true}.", "ping", Ping, temperature=0,
                        thinking="low", max_tokens=50)
        return {"ok": True, "latency_ms": int((time.time() - started) * 1000),
                "model": display_model(self.resolved_models()["turn"])}

    def status(self):
        return {"configured": self.configured, "models": self.public_models() if self.configured else {},
                "last_error": scrub(self.last_error)}


# ===================================================================== mock
class MockAI:
    """Deterministic stand-in used by tests and local dev (STAFF_AI_MOCK=1)."""

    configured = True
    last_error = ""

    def resolved_models(self):
        return {k: "mock" for k in PREFERENCES}

    def public_models(self):
        return {k: display_model("mock") for k in PREFERENCES}

    def ping(self):
        return {"ok": True, "latency_ms": 1, "model": display_model("mock")}

    def status(self):
        return {"configured": True, "models": self.public_models(), "last_error": "", "mock": True}

    def structured(self, role, system, user, schema, *, hint=None, parts=None, **kw):
        from . import schemas as sc
        hint = hint or {}
        name = schema.__name__
        if name == "InterviewPlan":
            topics = [sc.PlanTopic(id=t["id"], title=t["text"][:60], intent=t.get("intent", ""), required=bool(t.get("required", True)),
                                   probes=[f"Mock probe {i} on {t['text'][:30]}" for i in range(3)],
                                   listen_for=["specific examples"], max_questions=2)
                      for t in hint.get("topics", [])]
            return sc.InterviewPlan(candidate_summary="Mock summary of the candidate.", role_fit_hypotheses=["Fits"],
                                    things_to_verify=["Experience claims"], opening_approach="Warm and brief",
                                    topics=topics, target_questions=hint.get("target_questions", 6))
        if name == "TurnOut":
            turn_no = hint.get("turn_count", 0)
            pending = hint.get("pending", [])
            end = bool(hint.get("force_end")) or turn_no >= hint.get("max_turns", 6) or (not pending and turn_no >= 3)
            topic_id = pending[0] if pending else (hint.get("all_topics") or ["bg"])[0]
            updates = [sc.TopicUpdate(topic_id=topic_id, status="covered", depth=2)] if turn_no and not end else []
            code = hint.get("language_code") or "en"
            if code != "en":                         # dev mode: speak the fixed lines in the chosen language
                from . import languages
                reply = (languages.line(code, "closing", name="") if end else
                         languages.line(code, "opening", name="", persona="Noah") if turn_no == 0 else
                         languages.line(code, "fallback"))
            elif end:
                reply = "Thank you for your time. The hiring team will review the recording and be in touch."
            elif turn_no == 0:
                reply = "Hi, I'm Noah, an AI interviewer. To start, could you tell me a little about yourself?"
            else:
                reply = f"Thanks. Mock follow-up number {turn_no} about {topic_id}."
            return sc.TurnOut(answer_assessment="Mock assessment.", answer_quality=3,
                              move="wrap_up" if end else ("opening" if turn_no == 0 else "follow_up"),
                              topic_id=topic_id, topic_updates=updates, reply=reply, end_interview=end,
                              integrity_flag="")
        if name == "Evaluation":
            dims = [sc.DimensionScore(name=n, score=7, rationale="Mock rationale.")
                    for n in ("knowledge", "communication", "reasoning", "confidence", "role_fit")]
            return sc.Evaluation(overall_score=74, recommendation="yes", confidence_in_assessment="medium",
                                 confidence_note="Mock evaluation.", summary="Mock summary.", suitability="Suitable.",
                                 dimensions=dims, topic_assessments=[], strengths=["Clear communicator"],
                                 concerns=["None significant"], red_flags=[], suggested_followups=["Ask about scale."],
                                 integrity_notes="No issues.")
        if name == "FaceCheck":
            return sc.FaceCheck(face_count=1, single_live_person=True, screen_or_printed_photo_suspected=False,
                                face_clearly_visible=True, notes="mock")
        if name == "QuestionSuggestions":
            return sc.QuestionSuggestions(
                questions=[sc.SuggestedQuestion(text="Tell me about a project you are proud of.", intent="Depth", required=True, weight=3)],
                criteria=[sc.SuggestedCriterion(label="Communication", description="Clear and structured")],
                objectives="Assess fit.")
        raise AIError(f"mock has no handler for {name}")

    def transcribe(self, audio, mime, hint_terms="", language=None):
        from . import languages
        if language and languages.resolve(language) != "en":
            return languages.line(language, "fallback")           # native-script text so dev mode exercises Indic end to end
        return "this is a mock transcript"

    def tts(self, text, voice=None):
        import math
        rate, secs = 24000, 0.25
        pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                       for i in range(int(rate * secs)))
        return pcm_to_wav(pcm)

    def check_face(self, jpeg_bytes):
        return self.structured("vision", "", "", __import__("staff.schemas", fromlist=["FaceCheck"]).FaceCheck)


_service = None


def get_ai():
    global _service
    if _service is None:
        _service = MockAI() if get_settings().ai_mock else GeminiService()
    return _service


def reset_ai():
    global _service
    _service = None
