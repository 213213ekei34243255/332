"""Decision emails (passed / rejected) and the interview-received thank-you. Dry-run mail: nothing is sent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_foyer_access import interview_with_video  # noqa: F401,E402
from test_staff_flow import admin, apply_as_candidate, client, env, make_position, run_face_check  # noqa: F401,E402


def emails_for(admin, env, kind=None):
    env[1].mail.wait_idle()
    items = admin.get("/staff/api/emails").get_json()["items"]
    return [e for e in items if kind is None or e["kind"] == kind]


def make_application(client, admin):
    pos = make_position(admin)
    return apply_as_candidate(client, pos["id"]).get_json()["id"]


def test_preview_has_the_management_call_back_and_school_meet_wording(client, admin):
    app_id = make_application(client, admin)
    passed = admin.get(f"/staff/api/applications/{app_id}/decision-email?decision=shortlisted").get_json()["email"]
    assert passed["to"] == "priya@example.com" and passed["kind"] == "decision_passed"
    assert "passed the first round" in passed["message"]
    assert "The Management will call you back" in passed["message"]
    assert "school meet at the college" in passed["message"]
    assert "Senior Support Engineer" in passed["subject"]
    # hired gets the same "passed the first round" message (it must not claim employment)
    hired = admin.get(f"/staff/api/applications/{app_id}/decision-email?decision=hired").get_json()["email"]
    assert hired["message"] == passed["message"] and "hired" not in hired["message"].lower().replace("hiring", "")
    rejected = admin.get(f"/staff/api/applications/{app_id}/decision-email?decision=rejected").get_json()["email"]
    assert rejected["kind"] == "decision_rejected" and "not to move forward" in rejected["message"]
    assert "Management" not in rejected["message"] and "school meet" not in rejected["message"]
    assert admin.get(f"/staff/api/applications/{app_id}/decision-email?decision=on_hold").status_code == 400


def test_saving_a_decision_without_notify_sends_nothing(env, client, admin):
    app_id = make_application(client, admin)
    r = admin.patch(f"/staff/api/applications/{app_id}", json={"decision": "rejected"})
    assert r.status_code == 200 and r.get_json()["application"]["decision"] == "rejected" and r.get_json()["email"] is None
    assert emails_for(admin, env, "decision_rejected") == []


def test_notify_sends_the_edited_message_with_greeting_and_signoff(env, client, admin):
    app_id = make_application(client, admin)
    custom = "We are delighted to tell you that you passed.\n\nThe Management will call you back soon.\n<b>not bold</b>"
    r = admin.patch(f"/staff/api/applications/{app_id}", json={
        "decision": "shortlisted", "notify": True, "message": custom, "subject": "Good news\r\nBcc: evil@x.test"})
    out = r.get_json()
    assert r.status_code == 200 and out["email"]["kind"] == "decision_passed"
    sent = emails_for(admin, env, "decision_passed")
    assert len(sent) == 1 and sent[0]["status"] == "sent" and sent[0]["to_addr"] == "priya@example.com"
    assert "\n" not in sent[0]["subject"] and "Bcc" not in sent[0]["subject"].split("Good news")[0]
    from staff.mailer import render_message
    subject, text, html = render_message(name="Priya Nair", org_name="Org", subject="s", message=custom, preheader="p")
    assert text.startswith("Dear Priya,") and "Kind regards,\nThe Org Recruitment Team" in text
    assert "<b>not bold</b>" not in html and "&lt;b&gt;not bold&lt;/b&gt;" in html          # message is escaped


def test_default_message_is_used_when_none_given_and_duplicates_are_skipped(env, client, admin):
    app_id = make_application(client, admin)

    def decide(decision, **extra):
        return admin.patch(f"/staff/api/applications/{app_id}",
                           json=dict({"decision": decision, "notify": True}, **extra)).get_json()["email"]

    first = decide("shortlisted")
    assert first["kind"] == "decision_passed"
    assert decide("hired") == {"skipped": "already_sent", "kind": "decision_passed"}      # no second "you passed" mail
    assert decide("shortlisted", force=True)["kind"] == "decision_passed"                  # explicit resend works
    assert decide("on_hold") == {"skipped": "no_email_for_decision"}
    assert decide("rejected")["kind"] == "decision_rejected"                               # a different outcome is a different mail
    sent = emails_for(admin, env)
    assert sorted(e["kind"] for e in sent if e["kind"].startswith("decision_")) == [
        "decision_passed", "decision_passed", "decision_rejected"]
    # the preview now warns that decision emails were already sent
    prev = admin.get(f"/staff/api/applications/{app_id}/decision-email?decision=rejected").get_json()["email"]
    assert {e["kind"] for e in prev["earlier"]} == {"decision_passed", "decision_rejected"}


def test_clearing_a_decision_never_emails(env, client, admin):
    app_id = make_application(client, admin)
    r = admin.patch(f"/staff/api/applications/{app_id}", json={"decision": "", "notify": True})
    assert r.get_json()["email"] is None and emails_for(admin, env, "decision_passed") == []


def test_interview_finished_sends_one_thank_you_but_not_for_abandoned_sessions(env, client, admin):
    app_id, iv, _ = interview_with_video(client, admin)
    en = env[1].db.one("SELECT * FROM enrollments WHERE application_id = ?", (app_id,), "enrollments")
    env[1].finish_interview(en, reason="completed")
    env[1].finish_interview(en, reason="completed")                    # finishing twice must not email twice
    done = emails_for(admin, env, "interview_done")
    assert len(done) == 1 and done[0]["to_addr"] == "priya@example.com" and done[0]["status"] == "sent"
    assert "We received your interview" in done[0]["subject"]
    # a stale session closed by the recovery sweep is not a completed interview: no email
    app2 = apply_as_candidate(client, make_position(admin)["id"], email="second@example.com").get_json()["id"]
    code = admin.post(f"/staff/api/applications/{app2}/invite", json={}).get_json()["enrollment"]["code"]
    tok = client.post("/api/recruit/enroll/lookup", json={"code": code}).get_json()["token"]
    assert run_face_check(client, tok).status_code == 200
    client.post("/api/recruit/interview/start", headers={"Authorization": f"Bearer {tok}"})
    en2 = env[1].db.one("SELECT * FROM enrollments WHERE application_id = ?", (app2,), "enrollments")
    env[1].finish_interview(en2, reason="abandoned")
    assert [e["to_addr"] for e in emails_for(admin, env, "interview_done")] == ["priya@example.com"]
