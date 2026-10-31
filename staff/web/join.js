import { $, $$, h, mount, request, ApiError, sleep, fmtDuration } from './ui.js';
import { AudioHub, SpeechIn, SpeechOut } from './voice.js';
import { InterviewRecorder, UploadQueue } from './recorder.js';
import { FaceEngine, FaceVerifier, PresenceMonitor, TASK_HELP } from './face.js';

const S = {
  token: null, lookup: null, stream: null, hub: null, camId: '', micId: '', start: null,
  engine: new FaceEngine(), faceReady: false, active: false, phase: 'idle',
  speechOut: null, speechIn: null, recorder: null, queue: null, presence: null,
  events: [], eventTimer: null, timerInt: null, orbInt: null, startedAt: 0, durationMin: 15,
  overlay: { who: '', text: '' }, persona: 'Interviewer', micOk: false, meterInt: null,
  finishing: false, typed: null, repeat: false, hiddenAt: 0,
};

const api = (path, opts = {}) => request(`/api/recruit/${path}`, { token: S.token, ...opts });

// ------------------------------------------------------------------ stage switching
function show(name) {
  $$('[data-stage]').forEach((el) => { el.hidden = el.id !== `stage-${name}`; });
  window.scrollTo({ top: 0 });
}
function fatal(message) { $('#fatal-msg').textContent = message; show('error'); }
$('#fatal-retry').addEventListener('click', () => location.reload());

// ------------------------------------------------------------------ 1. code
const codeInput = $('#code');
codeInput.addEventListener('input', () => {
  const raw = codeInput.value.replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 8);
  codeInput.value = raw.length > 4 ? `${raw.slice(0, 4)}-${raw.slice(4)}` : raw;
});
$('#code-form').addEventListener('submit', (e) => { e.preventDefault(); lookup(codeInput.value); });

async function lookup(code) {
  const err = $('#code-error'); err.hidden = true;
  if (code.replace(/[^A-Za-z0-9]/g, '').length !== 8) { err.hidden = false; err.textContent = 'Please enter the 8-character code from your email (like ABCD-2345).'; return; }
  const btn = $('#code-btn'); btn.disabled = true; btn.textContent = 'Checking…';
  try {
    const data = await request('/api/recruit/enroll/lookup', { method: 'POST', json: { code } });
    S.token = data.token; S.lookup = data; S.persona = data.interview.persona || 'Interviewer';
    S.durationMin = data.interview.duration_minutes;
    $('#org').textContent = data.org;
    document.title = `Interview — ${data.position.title}`;
    $('#welcome-title').textContent = `Welcome, ${data.applicant.first_name}`;
    $('#welcome-sub').textContent = `${data.position.title}${data.position.department ? ' · ' + data.position.department : ''} — interviewed by ${S.persona}, an AI interviewer.`;
    $('#w-duration').textContent = data.interview.duration_minutes;
    history.replaceState(null, '', location.pathname);
    show('welcome');
  } catch (e) {
    err.hidden = false; err.textContent = e.message;
  } finally { btn.disabled = false; btn.textContent = 'Continue'; }
}
{
  const preset = new URLSearchParams(location.search).get('code');
  if (preset) { codeInput.value = preset; codeInput.dispatchEvent(new Event('input')); lookup(codeInput.value); }
}

// ------------------------------------------------------------------ 2. welcome
$('#consent').addEventListener('change', (e) => { $('#welcome-next').disabled = !e.target.checked; });
$('#welcome-next').addEventListener('click', async () => {
  try {
    S.hub = new AudioHub();
    await S.hub.resume();                      // created inside the click so audio is allowed to play
  } catch { /* checked in the device stage */ }
  show('devices');
  startDevices();
});

// ------------------------------------------------------------------ 3. devices
function checksList(items) {
  mount($('#checks'), items.map(([ok, label]) => h('li', { class: ok === null ? 'na' : ok ? 'ok' : 'bad' }, ok === null ? '•' : ok ? '✓' : '✗', ' ', label)));
}

async function openStream() {
  S.stream?.getTracks().forEach((t) => t.stop());
  S.stream = await navigator.mediaDevices.getUserMedia({
    video: { width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 }, facingMode: 'user', ...(S.camId ? { deviceId: { exact: S.camId } } : {}) },
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1, ...(S.micId ? { deviceId: { exact: S.micId } } : {}) },
  });
  for (const id of ['preview', 'face-video', 'room-video']) { const v = $(`#${id}`); v.srcObject = S.stream; v.play().catch(() => {}); }
  S.hub.attachMic(S.stream);
}

async function startDevices() {
  const err = $('#devices-error'); err.hidden = true;
  const caps = [
    [!!navigator.mediaDevices?.getUserMedia, 'Camera and microphone access is supported'],
    [typeof MediaRecorder !== 'undefined', 'Video recording is supported'],
    [!!(window.AudioContext || window.webkitAudioContext), 'Audio playback is supported'],
  ];
  if (!caps.every(([ok]) => ok)) {
    err.hidden = false; err.textContent = 'This browser can’t run the interview. Please use the latest Chrome, Edge or Safari on a laptop or desktop.';
    checksList(caps); return;
  }
  try { await openStream(); }
  catch (e) {
    err.hidden = false;
    err.textContent = e.name === 'NotAllowedError' ? 'Camera or microphone permission was blocked. Click the camera icon in the address bar, allow both, then reload this page.'
      : e.name === 'NotFoundError' ? 'No camera or microphone was found. Connect one and reload.' : `Couldn’t open your camera/microphone (${e.name}).`;
    return;
  }
  const devs = await navigator.mediaDevices.enumerateDevices();
  const fill = (sel, kind, current) => {
    const list = devs.filter((d) => d.kind === kind);
    mount(sel, list.map((d, i) => h('option', { value: d.deviceId, selected: d.deviceId === current }, d.label || `${kind === 'videoinput' ? 'Camera' : 'Microphone'} ${i + 1}`)));
  };
  fill($('#cam-select'), 'videoinput', S.stream.getVideoTracks()[0]?.getSettings().deviceId);
  fill($('#mic-select'), 'audioinput', S.stream.getAudioTracks()[0]?.getSettings().deviceId);
  const sr = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  checksList([...caps, [true, 'Camera is working'], [S.micOk || null, S.micOk ? 'Microphone is picking up your voice' : 'Microphone: say something to test'],
    [sr ? true : null, sr ? 'Live voice recognition available' : 'Using cloud voice recognition (this browser has none built in)']]);
  S.micOk = false; $('#devices-next').disabled = true;
  clearInterval(S.meterInt);
  let peak = 0;
  S.meterInt = setInterval(() => {
    const lvl = S.hub.micLevel();
    peak = Math.max(peak * 0.9, lvl);
    $('#mic-bar').style.width = `${Math.min(100, lvl * 500)}%`;
    if (lvl > 0.025 && !S.micOk) {
      S.micOk = true; $('#devices-next').disabled = false; $('#mic-hint').textContent = '— great, we can hear you';
      checksList([...caps, [true, 'Camera is working'], [true, 'Microphone is picking up your voice'], [sr ? true : null, sr ? 'Live voice recognition available' : 'Using cloud voice recognition']]);
    }
  }, 60);
}
$('#cam-select').addEventListener('change', async (e) => { S.camId = e.target.value; await openStream().catch(() => {}); });
$('#mic-select').addEventListener('change', async (e) => { S.micId = e.target.value; await openStream().catch(() => {}); });

function ensureSpeech() {
  if (S.speechOut) return;
  const useServer = S.lookup.interview.tts;
  S.speechOut = new SpeechOut({
    hub: S.hub, lang: S.lookup.interview.speech_lang || 'en-IN',
    fetchTts: useServer ? async (text) => {
      const res = await fetch('/api/recruit/interview/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${S.token}` }, body: JSON.stringify({ text }),
      });
      if (!res.ok) { const e = new Error('tts failed'); e.status = res.status; throw e; }
      return res.arrayBuffer();
    } : null,
  });
  S.speechIn = new SpeechIn({
    hub: S.hub, stream: S.stream, lang: S.lookup.interview.speech_lang || 'en-IN',
    transcribe: async (blob) => (await request('/api/recruit/interview/transcribe', { method: 'POST', body: blob, headers: { 'Content-Type': blob.type || 'audio/webm' }, token: S.token, timeout: 45000 })).text,
  });
}

$('#speaker-test').addEventListener('click', async () => {
  ensureSpeech(); await S.hub.resume();
  const st = $('#speaker-status'); st.textContent = 'Playing…';
  await S.speechOut.speak(`Hello ${S.lookup.applicant.first_name}. This is a quick sound check. If you can hear me, you're all set.`);
  st.textContent = 'Did you hear it? If not, check your volume or output device.';
});

$('#devices-next').addEventListener('click', async () => {
  clearInterval(S.meterInt);
  ensureSpeech();
  if (S.lookup.stage === 'interview') return startPrep();      // face check already passed earlier
  show('face'); startFace();
});

// ------------------------------------------------------------------ 4. face verification
async function startFace() {
  const err = $('#face-error'); err.hidden = true;
  const startBtn = $('#face-start'); startBtn.disabled = true;
  $('#face-load').hidden = false; $('#face-instruction').textContent = 'Preparing…';
  try { await S.engine.load(); S.faceReady = true; }
  catch (e) {
    console.error(e);
    err.hidden = false; err.textContent = 'Face detection could not load. Check your connection and try again.';
    startBtn.disabled = false; startBtn.textContent = 'Retry'; $('#face-load').hidden = true; return;
  }
  $('#face-load').hidden = true; startBtn.disabled = false; startBtn.textContent = 'Start verification';
  $('#face-instruction').textContent = 'Position your face inside the oval, then press Start.';
}

$('#face-start').addEventListener('click', async () => {
  if (!S.faceReady) return startFace();
  const err = $('#face-error'); err.hidden = true;
  const btn = $('#face-start'); btn.disabled = true; $('#face-spinner').hidden = false;
  const oval = $('#oval'); oval.className = 'oval';
  try {
    const ch = await api('face/challenge', { method: 'POST' });
    mount($('#face-steps'), ch.tasks.map((t) => h('li', { dataset: { task: t } }, h('span', { class: 'dot' }), TASK_HELP[t])));
    const verifier = new FaceVerifier({
      engine: S.engine, video: $('#face-video'),
      onInstruction: (t) => { $('#face-instruction').textContent = t; },
      onWarning: (w) => { $('#face-warning').textContent = w; oval.className = `oval${w ? ' bad' : ''}`; },
      onTask: (name, st) => {
        const li = $(`#face-steps [data-task="${name}"]`); if (!li) return;
        li.classList.toggle('active', st === 'start'); li.classList.toggle('done', st === 'done');
        if (st === 'done') li.querySelector('.dot').textContent = '✓';
      },
    });
    const result = await verifier.run(ch.tasks);
    oval.className = 'oval ok';
    $('#face-instruction').textContent = 'Verifying…';
    const form = new FormData();
    form.append('photo', result.photo, 'face.jpg');
    form.append('result', JSON.stringify({ nonce: ch.nonce, detector: 'mediapipe', steps: result.steps, metrics: result.metrics }));
    await api('face/submit', { method: 'POST', form });
    $('#face-instruction').textContent = 'Verified ✓';
    await sleep(700);
    startPrep();
  } catch (e) {
    oval.className = 'oval bad';
    err.hidden = false; err.textContent = e instanceof ApiError ? e.message : (e.message || 'Verification failed.');
    if (e.status === 429 || e.status === 401) btn.textContent = 'Contact the hiring team';
    else { btn.disabled = false; btn.textContent = 'Try again'; }
    $('#face-instruction').textContent = 'Let’s try again.';
  } finally { $('#face-spinner').hidden = true; if (!btn.textContent.startsWith('Contact')) btn.disabled = false; }
});

// ------------------------------------------------------------------ 5. prepare
async function startPrep() {
  show('prep');
  $('#prep-error').hidden = true; $('#prep-retry').hidden = true; $('#prep-begin').hidden = true; $('#prep-spinner').hidden = false;
  $('#prep-title').textContent = 'Preparing your interview…';
  $('#prep-sub').textContent = 'Your interviewer is reading the role and your background. This takes a few seconds.';
  try {
    if (!S.faceReady) S.engine.load().then(() => { S.faceReady = true; }).catch(() => {});
    S.start = await api('interview/start', { method: 'POST', timeout: 120000 });
    $('#prep-spinner').hidden = true;
    $('#prep-title').textContent = S.start.resumed ? 'Welcome back' : 'You’re all set';
    $('#prep-sub').textContent = `${S.persona} will speak first. Answer out loud in your own words and pause when you’re finished — the interviewer replies after a short silence. Take your time.`;
    $('#prep-begin').hidden = false;
  } catch (e) {
    $('#prep-spinner').hidden = true;
    const box = $('#prep-error'); box.hidden = false; box.textContent = e.message; $('#prep-retry').hidden = false;
    $('#prep-title').textContent = 'We couldn’t start yet';
  }
}
$('#prep-retry').addEventListener('click', startPrep);
$('#prep-begin').addEventListener('click', async () => { await S.hub.resume(); beginRoom(); });

// ------------------------------------------------------------------ 6. interview room
function setPhase(phase, label) {
  S.phase = phase;
  $('#stage-room').dataset.phase = phase;
  $('#phase').textContent = label || ({ speaking: `${S.persona} is speaking…`, listening: 'Your turn — I’m listening', thinking: 'Thinking…', idle: '' }[phase] || '');
  $('#btn-skip').hidden = phase !== 'speaking';
  $('#btn-done').hidden = phase !== 'listening';
  $('#btn-repeat').hidden = phase !== 'listening';
  $('#btn-type').hidden = !(phase === 'listening' && S.lookup.interview.allow_typing);
}

function addLog(who, text) {
  const log = $('#log');
  log.append(h('div', { class: `line ${who === 'me' ? 'me' : ''}` }, h('b', null, who === 'me' ? S.lookup.applicant.first_name : S.persona), text));
  log.scrollTop = log.scrollHeight;
}

function banner(msg) { const b = $('#room-banner'); b.hidden = !msg; b.textContent = msg || ''; }
function queueEvent(ev) { S.events.push({ ...ev }); }

async function flushEvents() {
  if (!S.events.length) return;
  const batch = S.events.splice(0, 50);
  try { await api('interview/events', { method: 'POST', json: { events: batch } }); } catch { S.events.unshift(...batch); }
}

async function beginRoom() {
  show('room');
  S.active = true;
  S.startedAt = Date.now() - (S.start.elapsed_ms || 0);
  const cfg = S.start.config;
  S.durationMin = cfg.duration_minutes;
  $('#persona').textContent = S.persona; $('#rec-chip').hidden = false; $('#timer-chip').hidden = false;
  // the last interviewer line is logged when it is (re-)spoken by the loop, so don't log it twice
  const history = S.start.turns.slice();
  if (history.length && history[history.length - 1].role === 'ai') history.pop();
  history.forEach((t) => addLog(t.role === 'ai' ? 'ai' : 'me', t.text));
  setPhase('idle', 'Starting…');

  // recording: composited video + mixed audio, uploaded in ordered chunks
  S.queue = new UploadQueue({ segment: S.start.segment, upload: (seg, seq, blob) => api(`interview/video/chunk?segment=${seg}&seq=${seq}`, { method: 'POST', body: blob, headers: { 'Content-Type': S.recorder.baseMime }, timeout: 60000 }) });
  S.recorder = new InterviewRecorder({
    video: $('#room-video'), hub: S.hub, label: `${S.lookup.applicant.full_name} — ${S.lookup.position.title}`,
    videoBps: cfg.video_bps, audioBps: cfg.audio_bps, getOverlay: () => S.overlay,
    onChunk: (blob) => S.queue.push(blob), onError: (e) => banner(`Recording problem: ${e.message}`),
  });
  try { S.recorder.start(); }
  catch (e) { S.active = false; return fatal(e.message); }
  S.queue.onStatus = (q) => { if (q.failed) banner('Part of the recording could not be saved. Your interview will continue.'); else if (q.pending.length > 6) banner('Your connection is slow — the recording is catching up.'); else if (!S.finishing) banner(''); };

  // integrity signals (presence + tab focus), flushed to the server in batches
  if (S.faceReady) {
    S.presence = new PresenceMonitor({ engine: S.engine, video: $('#room-video'), startedAt: S.startedAt, onEvent: queueEvent,
      onState: (msg) => { const w = $('#presence-warn'); w.hidden = !msg; w.textContent = msg; } });
    S.presence.start();
  }
  S.eventTimer = setInterval(flushEvents, 20000);
  document.addEventListener('visibilitychange', onVisibility);
  window.addEventListener('beforeunload', onBeforeUnload);
  S.timerInt = setInterval(tickTimer, 1000);
  S.orbInt = setInterval(tickOrb, 60);
  tickTimer();

  await interviewLoop();
}

function onVisibility() {
  if (document.hidden) S.hiddenAt = Date.now();
  else if (S.hiddenAt) {
    const dur = Date.now() - S.hiddenAt; S.hiddenAt = 0;
    if (dur > 4000) queueEvent({ type: 'tab_hidden', t: Date.now() - S.startedAt - dur, duration_ms: dur });
  }
}
function onBeforeUnload(e) { if (S.active && !S.finishing) { e.preventDefault(); e.returnValue = ''; } }

function tickTimer() {
  const elapsed = (Date.now() - S.startedAt) / 1000;
  $('#timer-chip').textContent = `${fmtDuration(elapsed)} / ~${S.durationMin} min`;
}

function tickOrb() {
  let lvl = 0;
  if (S.phase === 'speaking') lvl = S.speechOut?.mode === 'server' && S.speechOut.speaking ? Math.min(1, S.hub.outLevel() * 7) : 0.25 + 0.2 * Math.sin(Date.now() / 140);
  else if (S.phase === 'listening') lvl = Math.min(1, (S.speechIn?.level || 0) * 9);
  $('#orb').style.setProperty('--lvl', lvl.toFixed(2));
}

function setProgress(p) {
  if (!p) return;
  const pct = Math.min(100, Math.round(Math.max(p.answered / Math.max(1, p.target), (p.elapsed_ms / 60000) / Math.max(1, p.duration_minutes)) * 100));
  $('#progress-bar').style.width = `${pct}%`;
  $('#progress-text').textContent = `${p.answered} answer${p.answered === 1 ? '' : 's'} so far`;
}

async function speak(text) {
  S.overlay = { who: S.persona, text };
  $('#ai-caption').textContent = text;
  $('#me-caption').textContent = '';
  setPhase('speaking');
  addLog('ai', text);
  await S.speechOut.speak(text);
}

// One candidate turn: resolves with {kind, text, meta}.
async function listenOnce() {
  S.typed = null; S.repeat = false;
  setPhase('listening');
  $('#me-caption').textContent = '';
  S.overlay = { who: S.lookup.applicant.first_name, text: '' };
  const onInterim = (e) => { $('#me-caption').textContent = e.detail; S.overlay = { who: S.lookup.applicant.first_name, text: e.detail.slice(-170) }; };
  S.speechIn.addEventListener('interim', onInterim);
  try {
    const res = await S.speechIn.listen({ maxMs: 150000, noSpeechMs: 24000 });
    if (S.typed) return { kind: 'typed', text: S.typed, meta: {} };
    if (S.repeat) return { kind: 'repeat', text: '', meta: {} };
    if (res.reason === 'cancelled') return { kind: 'cancel', text: '', meta: {} };
    if (!res.text) return { kind: 'silence', text: '', meta: {}, heard: res.speechMs > 700 };
    return { kind: 'answer', text: res.text, meta: { delay_ms: res.delayMs !== null ? Math.round(res.delayMs) : null, stt: { source: res.source, speech_ms: Math.round(res.speechMs) } } };
  } finally { S.speechIn.removeEventListener('interim', onInterim); }
}

async function submitTurn(body) {
  for (let attempt = 1; ; attempt++) {
    try { const out = await api('interview/turn', { method: 'POST', json: body, timeout: 75000 }); banner(''); return out; }
    catch (e) {
      if (e.status === 409 && e.data?.expected_idx !== undefined) throw e;
      if ([401, 403, 404, 422].includes(e.status) || attempt > 24) throw e;
      banner(`Connection hiccup — retrying (${attempt})…`);
      await sleep(Math.min(1200 * attempt, 8000));
    }
  }
}

async function interviewLoop() {
  let turnIdx = S.start.turns.length;
  let toSpeak = S.start.last_ai_text;
  let done = !!S.start.done;
  try {
    while (S.active) {
      await speak(toSpeak);
      if (!S.active) return;
      if (done) break;
      await sleep(350);                                   // let the speaker tail die out before opening the mic
      let turn;
      do { turn = await listenOnce(); } while (turn.kind === 'cancel' && S.active);
      if (!S.active) return;
      setPhase('thinking');
      S.overlay = { who: S.persona, text: '…' };
      if (turn.kind === 'answer' || turn.kind === 'typed') { addLog('me', turn.text); $('#me-caption').textContent = turn.text; }
      else if (turn.kind === 'silence') $('#me-caption').textContent = turn.heard ? 'I didn’t quite catch that…' : '';
      let out;
      try {
        out = await submitTurn({ text: turn.text, kind: turn.kind, turn_idx: turnIdx, meta: turn.meta });
      } catch (e) {
        if (e.status === 409 && e.data?.expected_idx !== undefined) { turnIdx = e.data.expected_idx; continue; }
        throw e;
      }
      turnIdx = out.turn_idx + 1; toSpeak = out.reply; done = out.end; setProgress(out.progress);
    }
    if (done) await finishInterview('completed');
  } catch (e) {
    console.error(e);
    if (S.active && !S.finishing) { banner(''); await finishInterview('error'); }
  }
}

$('#btn-done').addEventListener('click', () => S.speechIn?.forceDone());
$('#btn-repeat').addEventListener('click', () => { S.repeat = true; S.speechIn?.cancel(); });
$('#btn-skip').addEventListener('click', () => S.speechOut?.stop());
$('#btn-type').addEventListener('click', () => { $('#type-box').hidden = false; $('#type-input').focus(); });
$('#type-cancel').addEventListener('click', () => { $('#type-box').hidden = true; });
$('#type-input').addEventListener('paste', () => queueEvent({ type: 'paste', t: Date.now() - S.startedAt, duration_ms: 0 }));
$('#type-send').addEventListener('click', () => {
  const text = $('#type-input').value.trim();
  if (!text) return;
  S.typed = text; $('#type-input').value = ''; $('#type-box').hidden = true; S.speechIn?.cancel();
});
$('#btn-end').addEventListener('click', async () => {
  if (!confirm('End the interview now? You will not be able to continue it.')) return;
  S.speechOut?.stop(); S.speechIn?.cancel();
  await finishInterview('candidate_ended');
});

// ------------------------------------------------------------------ 7. finish
async function finishInterview(reason) {
  if (S.finishing) return;
  S.finishing = true; S.active = false;
  clearInterval(S.timerInt); clearInterval(S.orbInt); clearInterval(S.eventTimer);
  document.removeEventListener('visibilitychange', onVisibility);
  S.speechOut?.stop(); S.speechIn?.destroy(); S.presence?.stop();
  show('done'); $('#rec-chip').hidden = true;
  await flushEvents();
  try { await api('interview/finish', { method: 'POST', json: { reason } }); } catch (e) { console.error('finish failed', e); }
  await S.recorder?.stop();
  S.stream?.getTracks().forEach((t) => t.stop());
  await uploadAll();
}

async function uploadAll() {
  const bar = $('#upload-bar'), text = $('#upload-text'), retry = $('#upload-retry');
  retry.hidden = true;
  const total = () => Math.max(1, S.queue.chunks);
  const progress = setInterval(() => {
    const pct = Math.round(((S.queue.chunks - S.queue.pending.length) / total()) * 100);
    bar.style.width = `${pct}%`; text.textContent = `Saving your recording… ${pct}%`;
  }, 300);
  const ok = await S.queue.drain(120000);
  clearInterval(progress);
  if (ok) {
    try { await api('interview/video/complete', { method: 'POST', json: { segment: S.start.segment, chunks: S.queue.chunks, duration_ms: S.recorder.durationMs } }); } catch (e) { console.error(e); }
    window.removeEventListener('beforeunload', onBeforeUnload);
    bar.style.width = '100%'; text.textContent = '';
    $('#done-icon').textContent = '✓';
    $('#done-title').textContent = 'Thank you — you’re all done';
    $('#done-sub').textContent = 'Your interview and recording have been submitted. The hiring team will review them and be in touch. You can close this tab.';
  } else {
    $('#done-title').textContent = 'Your answers are saved';
    $('#done-sub').textContent = 'The video upload is incomplete because of the connection. Please keep this tab open and retry.';
    text.textContent = S.queue.lastError || 'Waiting for the connection…'; retry.hidden = false;
  }
}
$('#upload-retry').addEventListener('click', () => { S.queue.failed = false; S.queue._pump(); uploadAll(); });
