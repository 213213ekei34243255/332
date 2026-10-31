"""The interview engine: plan -> adaptive turns -> evaluation.

Gemini does the thinking (which question next, how deep to probe, how good the
answer was); this module is the *director*: it keeps coverage/time/budget state
in the database, enforces guardrails the model cannot be trusted with (never
end early with required topics untouched, never run past the hard limit, never
loop on silence) and post-processes the evaluation so scores are reproducible.
"""
import copy
import logging
import re

from . import prompts
from .gemini import AIError
from .schemas import Evaluation, InterviewPlan, TurnOut, clamp_int

log = logging.getLogger("staff.engine")

CANDIDATE_ANSWER_KINDS = ("answer", "typed")


def _first_name(application):
    return (application.get("full_name") or "there").split()[0]


def clean_spoken(text, limit=700):
    """Make model output safe to speak: no markdown, no stage directions."""
    text = re.sub(r"[*_`#>]+", "", text or "")
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text)  # [pause], (smiles)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class InterviewEngine:
    def __init__(self, ai, org_name):
        self.ai = ai
        self.org = org_name

    # ------------------------------------------------------------------ state
    def init_state(self, position, application):
        qs = (position.get("config") or {}).get("questions") or []
        topics = [{"id": "bg1", "text": "Background and motivation", "intent": "Understand the candidate and why this role",
                   "required": True, "weight": 3, "status": "untouched", "depth": 0, "asked": 0}]
        for q in qs:
            topics.append({"id": q["id"], "text": q["text"], "intent": q.get("intent", ""),
                           "required": bool(q.get("required", True)), "weight": q.get("weight", 3),
                           "status": "untouched", "depth": 0, "asked": 0})
        settings = prompts.interview_settings(position)
        return {"plan": {}, "topics": topics, "turn_count": 0, "ai_turn_count": 0, "silent_streak": 0,
                "flags": [], "settings": settings, "closing": False}

    def start(self, position, application):
        """Build the plan and the opening line. Returns (state, opening_dict)."""
        state = self.init_state(position, application)
        settings = state["settings"]
        try:
            system, user = prompts.plan_prompt(position, application, settings, self.org)
            plan = self.ai.structured(
                "turn", system, user, InterviewPlan, temperature=0.5, thinking="low", max_tokens=6000,
                hint={"topics": state["topics"], "target_questions": settings["target_questions"]})
            state["plan"] = plan.model_dump()
            known = {t["id"] for t in state["topics"]}
            for pt in plan.topics:  # adopt the plan's extra (background/adaptive) topics
                if pt.id not in known:
                    state["topics"].append({"id": pt.id, "text": pt.title, "intent": pt.intent, "required": False,
                                            "weight": 2, "status": "untouched", "depth": 0, "asked": 0})
        except AIError:
            log.warning("plan generation failed; interviewing without a prepared plan")
        notes = prompts.director_notes(state, 0, settings)
        out = self._ask_model(position, application, state, [], prompts.opening_user(notes),
                              hint={"turn_count": 0, "pending": [t["id"] for t in state["topics"] if t["required"]],
                                    "all_topics": [t["id"] for t in state["topics"]], "max_turns": settings["max_turns"]})
        reply = clean_spoken(out.reply) or (
            f"Hi {_first_name(application)}, I'm {settings['persona']}, an AI interviewer. "
            "To start, could you tell me a bit about yourself and what drew you to this role?")
        state["ai_turn_count"] = 1
        self._mark_asked(state, "bg1")
        return state, {"reply": reply, "move": "opening", "topic_id": "bg1", "assessment": "", "flag": ""}

    # ------------------------------------------------------------------ turns
    def turn(self, position, application, state, prior_turns, text, kind, elapsed_ms):
        state = copy.deepcopy(state)
        settings = state.get("settings") or prompts.interview_settings(position)
        text = (text or "").strip()

        if kind == "silence":
            state["silent_streak"] = state.get("silent_streak", 0) + 1
        elif kind in CANDIDATE_ANSWER_KINDS and text:
            state["silent_streak"] = 0
            state["turn_count"] = state.get("turn_count", 0) + 1

        pending = [t["id"] for t in state["topics"] if t.get("required") and t.get("status") != "covered"]
        hard_stop = (state["turn_count"] >= settings["max_turns"]
                     or elapsed_ms >= settings["hard_limit_minutes"] * 60_000
                     or state.get("silent_streak", 0) >= 3)
        target_reached = (state["turn_count"] >= settings["target_questions"]
                          or elapsed_ms >= settings["duration_minutes"] * 60_000)
        wrap_now = hard_stop or (target_reached and not pending)
        instruction = self._direction(kind, state, pending, elapsed_ms, settings, wrap_now, hard_stop)

        notes = prompts.director_notes(state, elapsed_ms, settings, instruction)
        user = prompts.turn_user(prior_turns, text or "(no response)", kind, notes)
        hint = {"turn_count": state["turn_count"], "pending": pending, "max_turns": settings["max_turns"],
                "all_topics": [t["id"] for t in state["topics"]], "force_end": wrap_now}
        out = self._ask_model(position, application, state, prior_turns, user, hint=hint)

        end = bool(out.end_interview)
        # Guardrail 1: the model may not end while required topics remain and budget is left.
        if end and not wrap_now and pending and state["turn_count"] < settings["target_questions"]:
            retry_note = prompts.director_notes(
                state, elapsed_ms, settings,
                f"You tried to end the interview too early. Do NOT end. Ask about required topic {pending[0]} next.")
            hint["force_end"] = False
            out = self._ask_model(position, application, state, prior_turns,
                                  prompts.turn_user(prior_turns, text or "(no response)", kind, retry_note), hint=hint)
            end = bool(out.end_interview)
        # Guardrail 2: when we must wrap up, the reply must actually be a goodbye.
        if wrap_now and not end:
            out_reply = self._closing_text(application, settings)
            end = True
            out = out.model_copy(update={"reply": out_reply, "move": "wrap_up", "end_interview": True})

        reply = clean_spoken(out.reply) or self._fallback_reply(application, settings, end)
        self._apply_updates(state, out)
        if out.integrity_flag and out.integrity_flag.strip():
            state["flags"] = (state.get("flags", []) + [out.integrity_flag.strip()[:200]])[-10:]
        state["ai_turn_count"] = state.get("ai_turn_count", 0) + 1
        state["closing"] = end
        return state, {"reply": reply, "end": end, "move": out.move, "topic_id": out.topic_id,
                       "quality": clamp_int(out.answer_quality, 0, 4), "assessment": (out.answer_assessment or "")[:400],
                       "flag": (out.integrity_flag or "")[:200]}

    def _ask_model(self, position, application, state, prior_turns, user, hint):
        settings = state.get("settings") or prompts.interview_settings(position)
        system = prompts.turn_system(position, application, state, settings, self.org)
        return self.ai.structured("turn", system, user, TurnOut, temperature=0.7, thinking="low",
                                  max_tokens=2000, hint=hint)

    @staticmethod
    def _direction(kind, state, pending, elapsed_ms, settings, wrap_now, hard_stop):
        if hard_stop and state.get("silent_streak", 0) >= 3:
            return ("The candidate has been silent several times. Kindly say that you will end the session here and "
                    "that they can contact the hiring team, thank them, and set end_interview=true (move=wrap_up).")
        if wrap_now:
            return ("WRAP UP NOW: briefly acknowledge their last answer, thank them warmly, say the hiring team will "
                    "review the recording and be in touch, and set end_interview=true (move=wrap_up).")
        parts = []
        if kind == "silence":
            tail = ("or offer to move on to a different question." if state.get("silent_streak", 0) >= 2
                    else "giving them time.")
            parts.append("The candidate did not respond. Check in kindly and restate your last question more simply, " + tail)
        elif kind == "repeat":
            parts.append("The candidate asked you to repeat or rephrase. Restate your previous question in different, "
                         "simpler words (move=rephrase). Do not advance the topic.")
        elif kind == "typed":
            parts.append("The candidate typed this answer instead of speaking; treat it like a normal answer.")
        if pending and elapsed_ms >= 0.8 * settings["duration_minutes"] * 60_000:
            parts.append("Time is running short: prioritise the required topics not yet covered with ONE focused "
                         "question each; skip optional topics.")
        return " ".join(parts)

    @staticmethod
    def _closing_text(application, settings):
        return (f"Thank you, {_first_name(application)}. That's everything I wanted to cover. "
                "The hiring team will review your recording and be in touch. Take care!")

    @staticmethod
    def _fallback_reply(application, settings, end):
        if end:
            return InterviewEngine._closing_text(application, settings)
        return "Thanks for sharing that. Could you give me a specific example from your own experience?"

    @staticmethod
    def _mark_asked(state, topic_id):
        for t in state["topics"]:
            if t["id"] == topic_id:
                t["asked"] = t.get("asked", 0) + 1
                if t["status"] == "untouched":
                    t["status"] = "in_progress"

    def _apply_updates(self, state, out):
        by_id = {t["id"]: t for t in state["topics"]}
        for upd in out.topic_updates or []:
            t = by_id.get(upd.topic_id)
            if t:
                t["depth"] = max(t.get("depth", 0), clamp_int(upd.depth, 0, 3))
                if upd.status == "covered" or t["status"] == "untouched":
                    t["status"] = upd.status
        if out.move not in ("wrap_up", "rephrase") and out.topic_id in by_id:
            self._mark_asked(state, out.topic_id)
        # A topic asked 3+ times with depth >= 2 counts as covered even if the model forgot to say so.
        for t in state["topics"]:
            if t.get("asked", 0) >= 3 and t.get("depth", 0) >= 2:
                t["status"] = "covered"

    # ------------------------------------------------------------------ evaluation
    def evaluate(self, position, application, state, turns, events):
        answers = [t for t in turns if t["role"] == "candidate"
                   and (t.get("meta") or {}).get("kind", "answer") in CANDIDATE_ANSWER_KINDS and t["body"].strip()]
        if len(answers) < 2:
            return self._insufficient(len(answers))
        system, user = prompts.eval_prompt(position, application, state, turns, summarize_events(events), self.org)
        ev = self.ai.structured("eval", system, user, Evaluation, temperature=0.2, thinking="high", max_tokens=12000)
        return self._postprocess(ev, position, turns)

    @staticmethod
    def _insufficient(n):
        return {"overall_score": None, "recommendation": "no_data", "confidence_in_assessment": "low",
                "confidence_note": f"Only {n} candidate answer(s) were captured, which is not enough to assess.",
                "summary": "The interview ended before enough answers were recorded to produce a reliable assessment.",
                "suitability": "Not assessed", "dimensions": [], "topic_assessments": [], "strengths": [],
                "concerns": ["Interview too short to evaluate"], "red_flags": [], "suggested_followups":
                ["Consider inviting the candidate to retake the interview."], "integrity_notes": "", "model_overall": None}

    @staticmethod
    def _postprocess(ev, position, turns):
        data = ev.model_dump()
        crit = {c["label"].strip().lower(): c for c in (position.get("config") or {}).get("criteria") or []}
        dims = []
        for d in data["dimensions"]:
            d["score"] = clamp_int(d["score"], 0, 10)
            d["name"] = d["name"].strip()[:80]
            dims.append(d)
        core = ["knowledge", "communication", "reasoning", "confidence", "role_fit"]
        order = {n: i for i, n in enumerate(core)}
        dims.sort(key=lambda d: order.get(d["name"].lower(), 99))
        total_w, total = 0.0, 0.0
        for d in dims:
            key = d["name"].lower()
            w = prompts.EVAL_WEIGHTS.get(key) or (crit.get(key, {}).get("weight") or 3)
            total_w += w
            total += w * d["score"]
        model_overall = clamp_int(data["overall_score"], 0, 100)
        overall = round(10 * total / total_w) if total_w else model_overall
        data["model_overall"] = model_overall
        data["overall_score"] = overall
        mapped = ("strong_yes" if overall >= 80 else "yes" if overall >= 65 else "maybe" if overall >= 50
                  else "no" if overall >= 35 else "strong_no")
        ladder = ["strong_no", "no", "maybe", "yes", "strong_yes"]
        if abs(ladder.index(mapped) - ladder.index(data["recommendation"])) > 1:
            data["recommendation"] = mapped
        # Evidence must be a verbatim candidate quote; drop anything the model invented.
        candidate_text = " ".join(t["body"] for t in turns if t["role"] == "candidate").lower()
        norm = lambda s: re.sub(r"\s+", " ", re.sub(r"[^\w\s']", "", s.lower())).strip()
        hay = norm(candidate_text)
        for ta in data["topic_assessments"]:
            ta["score"] = clamp_int(ta["score"], -1, 10, -1)
            ta["evidence"] = [q for q in ta["evidence"] if norm(q) and norm(q) in hay][:3]
        data["dimensions"] = dims
        return data


def summarize_events(events):
    if not events:
        return "No presence or tab-activity signals were recorded."
    counts, durations = {}, {}
    for e in events:
        t = e.get("type", "event")
        counts[t] = counts.get(t, 0) + 1
        durations[t] = durations.get(t, 0) + int(e.get("duration_ms") or 0)
    labels = {"face_absent": "face not visible", "multiple_faces": "more than one face visible",
              "looking_away": "looking away from the screen", "tab_hidden": "left the interview tab",
              "paste": "pasted text into the answer box"}
    return "\n".join(f"- {labels.get(t, t)}: {n} time(s)" + (f", total {durations[t] // 1000}s" if durations[t] else "")
                     for t, n in counts.items())
