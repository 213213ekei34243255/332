"""Structured-output schemas shared by the Gemini engine and its mock.

Kept free of defaults and exotic JSON-schema features so Gemini's
response_schema accepts them; leniency lives in `coerce_*` helpers instead.
"""
from typing import List, Literal

from pydantic import BaseModel


class PlanTopic(BaseModel):
    id: str
    title: str
    intent: str
    required: bool
    probes: List[str]
    listen_for: List[str]
    max_questions: int


class InterviewPlan(BaseModel):
    candidate_summary: str
    role_fit_hypotheses: List[str]
    things_to_verify: List[str]
    opening_approach: str
    topics: List[PlanTopic]
    target_questions: int


class TopicUpdate(BaseModel):
    topic_id: str
    status: Literal["in_progress", "covered"]
    depth: int


class TurnOut(BaseModel):
    answer_assessment: str
    answer_quality: int
    move: Literal["opening", "follow_up", "probe_deeper", "clarify", "next_topic",
                  "redirect", "rephrase", "wrap_up"]
    topic_id: str
    topic_updates: List[TopicUpdate]
    reply: str
    end_interview: bool
    integrity_flag: str


class DimensionScore(BaseModel):
    name: str
    score: int
    rationale: str


class TopicAssessment(BaseModel):
    topic_id: str
    question_asked: str
    answer_summary: str
    score: int
    evidence: List[str]
    gaps: str


class Evaluation(BaseModel):
    overall_score: int
    recommendation: Literal["strong_yes", "yes", "maybe", "no", "strong_no"]
    confidence_in_assessment: Literal["low", "medium", "high"]
    confidence_note: str
    summary: str
    suitability: str
    dimensions: List[DimensionScore]
    topic_assessments: List[TopicAssessment]
    strengths: List[str]
    concerns: List[str]
    red_flags: List[str]
    suggested_followups: List[str]
    integrity_notes: str


class FaceCheck(BaseModel):
    face_count: int
    single_live_person: bool
    screen_or_printed_photo_suspected: bool
    face_clearly_visible: bool
    notes: str


class Ping(BaseModel):
    ok: bool


class SuggestedQuestion(BaseModel):
    text: str
    intent: str
    required: bool
    weight: int


class SuggestedCriterion(BaseModel):
    label: str
    description: str


class QuestionSuggestions(BaseModel):
    questions: List[SuggestedQuestion]
    criteria: List[SuggestedCriterion]
    objectives: str


def clamp_int(value, lo, hi, default=0):
    try:
        return max(lo, min(hi, int(round(float(value)))))
    except (TypeError, ValueError):
        return default
