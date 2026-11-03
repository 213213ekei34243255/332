# Staff recruitment system — Christ Junior College teaching & staff appointments

Applicant Enrollment → Job Selection → Interview Code → Automated Email → Face Verification →
AI Interview → Video Recording → AI Evaluation → Administrator Review.

Everything lives in the `staff/` package and is wired into `app.py` with one `init_staff(app)` call.
It does not touch the Veronica chatbot routes, and a failure to start it cannot take the site down.
The Foyer app is a client of this backend: its **`/staff` dashboard** (React, `src/staff/` in Foyer) reads and
writes everything here through Foyer's server (`/api/staff/*` -> `/staff/api/*`), `/apply` forwards here, and emailed
enrollment codes go to `/join`. **No video is ever stored in Foyer.** The dashboard served directly by this
backend at `/staff` keeps working as a standalone fallback (same API, cookie session instead of a bearer token).

| URL | Who | What |
|---|---|---|
| `/staff` | administrators | dashboard: positions, applications, codes, emails, interviews & videos, system (also available in Foyer at its own `/staff`) |
| `/apply` | applicants | pick a position, submit details + résumé |
| `/join?code=ABCD-2345` | applicants | device check → face verification → AI voice interview (recorded) |
| `/api/recruit/*` | applicant browser | public API (rate limited, token-scoped) |
| `/staff/api/*` | dashboards | admin API: signed cookie + CSRF header, or `Authorization: Bearer` (Foyer). Video/photo/resume use one-resource `?st=` links minted by `POST /staff/api/media-url` (1 h) |
| `POST /interview` | Foyer | existing endpoint, now answered by the Rexy model (falls back to the local model) |
| `/` | public | front door: apply, or enter an interview code (shown when `templates/index.html` is absent) |

## Setup (Render → Environment)

See `.env.example`. The minimum:

* `STAFF_ADMIN_PASSWORD` (or the existing `ADMIN_API_KEY`) — dashboard password.
* `REXY_API_KEY` (the AI key)
* `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=465`, `SMTP_USER`, **`SMTP_PASSWORD`**, `SMTP_FROM_NAME`

`SMTP_PASSWORD` must be a Google **App Password** (Google Account → Security → 2-Step Verification →
App passwords), not the normal account password. **Render free web services block outbound SMTP
(ports 25/465/587); use a paid instance** (the llama model + persistent disk this app already needs
means that is probably already the case). Use *System → Send test* in the dashboard to confirm.

Recordings go to `STAFF_DATA_DIR` (default `/var/data/staff`, i.e. the Render persistent disk that
`start.sh` already uses for the model). Without a persistent disk they are lost on redeploy.
Database tables (`staff_*`) are created automatically in `DATABASE_URL` (Postgres) or in a local SQLite
file when it is not set.

## Emails the system sends

| Email | When | Notes |
|---|---|---|
| Application received | applicant submits the form | automatic |
| Interview invitation | admin generates a code (or auto-invite) | personal code + instructions |
| New application alert | every application, if `STAFF_NOTIFY_EMAIL` is set | to the admin |
| Interview received | applicant finishes the interview (not for abandoned sessions) | automatic, sent once, never includes scores |
| Decision: passed the first round | admin marks **Shortlisted** or **Hired** | says the Management will call back and a school meet at the college will be arranged soon |
| Decision: not selected | admin marks **Rejected** | polite, no scores or AI feedback |

Decision emails are never silent: choosing a decision opens a dialog showing the exact subject and message (editable) with
"Email this to the applicant" ticked; untick it to save the decision without sending. "On hold" and clearing a decision
never send. The same email is not sent twice (a later "Hired" after "Shortlisted" is skipped), and everything appears in
the Email log. Edit the default wording in `default_decision_email()` in `staff/mailer.py`.

## Keep your data: persistence on Render (important)

A Render web service's own disk is **temporary**: it is erased on every deploy, restart and (free tier) spin-down.
Without the two settings below, positions, applications, interviews and recorded videos disappear. The dashboard
now shows a red warning (Overview and System) whenever it detects this.

1. **Database:** set `DATABASE_URL` to a Postgres database (Render Postgres, Neon, Supabase...). All staff tables
   (`staff_*`) are created automatically. Without it the system falls back to a SQLite file on the temporary disk.
2. **Videos, photos, resumes:** attach a **persistent disk** to the service in Render (Disks) mounted at `/var/data`
   (a paid-instance feature). Recordings are written to `/var/data/staff`. Without a mounted disk they are lost on redeploy.

## How the interview works

* **Languages** – every interview can run in English, Malayalam, Kannada, Tamil, Telugu or Hindi. The administrator sets a
  default per position (and can let each applicant choose); the interviewer speaks and listens in that language, answers
  may mix in English, and the final assessment is always written in English with verbatim quotes in the original script.
  Fixed lines (greeting, goodbye) live in `staff/languages.py`.
* **Plan** – before the first question, the Rexy model reads the position (skills, eligibility, objectives),
  the admin's topic seeds and the applicant's profile/résumé and writes a per-candidate plan: probe
  banks per topic ordered easy → hard, claims to verify, background angles.
* **Turns** – every answer goes to the Rexy model with a *director's briefing* (elapsed time, coverage and depth
  per topic, budget left). The model returns structured JSON: its private assessment of the answer, topic
  updates, and the sentence to speak. It rewrites topic seeds in its own words, follows up on what was
  actually said, and adapts difficulty. The server enforces what a model can't be trusted with: required
  topics get covered before it may end, a hard time/question cap, three silences end the session,
  candidate text is treated as untrusted (prompt-injection attempts are flagged, not obeyed).
* **Evaluation** – after the call a stronger model scores knowledge, communication, reasoning,
  confidence, role fit and every admin criterion 0–10 with evidence. Scores are recomputed server-side
  from the dimension scores (reproducible), quotes the model invented are dropped, and the dashboard
  shows confidence in the assessment. It is an aid: a human makes the decision.
* **Models** are resolved at runtime from `models.list()` (override with `STAFF_MODEL_*`), with automatic
  fallback when a model is retired or rate-limited.

## Voice, face, recording

* Half-duplex, hands-free turn-taking (`web/voice.js`): the mic is closed while the interviewer speaks;
  voice-activity detection + adaptive end-of-speech timing decide when the candidate is done; Web
  Speech recognition gives live captions and the audio segment is re-transcribed by the Rexy model when the
  browser engine fails or is missing (Firefox). Interviewer voice is Rexy Voice (mixed into the
  recording) with fallback to a sentence-chunked, voice-ranked browser voice.
* Face verification (`web/face.js`, MediaPipe Face Landmarker, vendored in `web/vendor/`, runs on-device):
  framing/lighting checks, then a server-chosen liveness challenge (blink / smile / open mouth / turn head).
  Only one verification photo is uploaded; the Rexy model double-checks it is a single live person. During the
  interview, face-missing / second-person / tab-switch signals are logged for reviewers (never auto-scored).
* Video is composited in the browser (camera + captions + both voices) and uploaded in ordered 4-second
  chunks, so a dropped connection never loses the whole recording; reloading resumes the interview.

## Security notes

* Recordings, photos and résumés are outside the web root and Foyer; only `GET /staff/api/recordings/<id>/stream`
  (admin session + Range support) serves them. Every view/download/delete is written to the audit log.
* Admin session: HMAC-signed `HttpOnly; SameSite=Strict; Secure` cookie scoped to `/staff`, CSRF header,
  login rate limit, constant-time password compare. Applicant tokens are signed, scoped, expiring.
* Strict CSP on all pages (no inline script/style), input validation + length caps everywhere,
  path-traversal-safe storage, honeypot + rate limits on the public form, résumé type sniffing.
* Enrollment codes: 8 chars from an unambiguous alphabet, single use, expiring, revocable, lookup throttled.

## Development & tests

```bash
python staff_dev.py                  # staff system only, http://127.0.0.1:5055/staff  (STAFF_AI_MOCK=1 for no key)
python -m pytest tests               # backend + AI layer (fake REST server through the real SDK)
STAFF_TEST_DATABASE_URL=postgresql://... python -m pytest tests/test_staff_flow.py   # same suite on Postgres
node tests/face_logic.test.mjs       # liveness state machines
```
