// In-browser face verification (MediaPipe Face Landmarker, vendored locally - no frames leave the device
// except the single verification photo the applicant explicitly submits).
//
//  * FaceVerifier  - guided liveness check: frame the face, then 2 server-chosen actions
//                    (blink / smile / open mouth / turn head). Captures the verification photo.
//  * PresenceMonitor - low-rate check during the interview: face missing, a second person, looking away.
import { sleep } from './voice.js';

const BASE = '/staff-static/vendor/mediapipe';

export class FaceEngine {
  constructor() {
    this.landmarker = null;
    this.probe = document.createElement('canvas');
    this.probe.width = 64; this.probe.height = 48;
    this.probeCtx = this.probe.getContext('2d', { willReadFrequently: true });
    this._lastTs = 0;
  }

  async load() {
    if (this.landmarker) return;
    const mod = await import(`${BASE}/vision_bundle.mjs`);
    const fileset = await mod.FilesetResolver.forVisionTasks(`${BASE}/wasm`);
    const make = (delegate) => mod.FaceLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: `${BASE}/face_landmarker.task`, delegate },
      runningMode: 'VIDEO', numFaces: 2, outputFaceBlendshapes: true, outputFacialTransformationMatrixes: false,
    });
    try { this.landmarker = await make('GPU'); }
    catch { this.landmarker = await make('CPU'); }
  }

  brightness(video) {
    try {
      this.probeCtx.drawImage(video, 0, 0, 64, 48);
      const d = this.probeCtx.getImageData(16, 12, 32, 24).data;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4) sum += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
      return sum / (d.length / 4);
    } catch { return 128; }
  }

  // -> { faces, box:{cx,cy,w,h}, turn, blink, smile, jaw, brightness }  (turn: -1..1, ~0 when facing the camera)
  analyze(video) {
    const out = { faces: 0, box: null, turn: 0, blink: 0, smile: 0, jaw: 0, brightness: 128 };
    if (!this.landmarker || video.readyState < 2 || !video.videoWidth) return out;
    out.brightness = this.brightness(video);
    const ts = Math.max(performance.now(), this._lastTs + 1);
    this._lastTs = ts;
    let res;
    try { res = this.landmarker.detectForVideo(video, ts); } catch { return out; }
    out.faces = res.faceLandmarks?.length || 0;
    if (!out.faces) return out;
    const lm = res.faceLandmarks[0];
    let minX = 1, maxX = 0, minY = 1, maxY = 0;
    for (const p of lm) { if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x; if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y; }
    out.box = { cx: (minX + maxX) / 2, cy: (minY + maxY) / 2, w: maxX - minX, h: maxY - minY };
    // yaw proxy: where the nose tip sits between the left and right face contour (0.5 = facing the camera)
    const left = lm[234], right = lm[454], nose = lm[1];
    const span = right.x - left.x;
    out.turn = span > 0.01 ? ((nose.x - left.x) / span - 0.5) * 2 : 0;
    const bs = {};
    for (const c of res.faceBlendshapes?.[0]?.categories || []) bs[c.categoryName] = c.score;
    out.blink = ((bs.eyeBlinkLeft || 0) + (bs.eyeBlinkRight || 0)) / 2;
    out.smile = ((bs.mouthSmileLeft || 0) + (bs.mouthSmileRight || 0)) / 2;
    out.jaw = bs.jawOpen || 0;
    return out;
  }
}

export class TaskTimeout extends Error {}

export const TASK_HELP = {
  center: 'Look straight at the camera and hold still',
  blink: 'Blink slowly, once',
  smile: 'Give us a natural smile',
  open_mouth: 'Open your mouth wide, like saying “ahh”',
  turn_head: 'Slowly turn your head to one side, then the other',
};

export class FaceVerifier {
  constructor({ engine, video, onInstruction, onWarning, onTask }) {
    this.engine = engine; this.video = video;
    this.say = onInstruction || (() => {}); this.warn = onWarning || (() => {}); this.onTask = onTask || (() => {});
    this.cancelled = false;
  }
  cancel() { this.cancelled = true; }

  async _frame() {
    await sleep(70);
    if (this.cancelled) throw new Error('cancelled');
    return this.engine.analyze(this.video);
  }

  static framingProblem(m) {
    if (m.faces === 0) return 'We can’t see your face — move into the frame.';
    if (m.faces > 1) return 'Only you should be in view. Please ask others to step away.';
    if (m.brightness < 45) return 'It’s too dark — face a window or lamp.';
    if (m.brightness > 225) return 'Too bright — avoid a strong light behind you.';
    if (m.box.w < 0.17) return 'Move a little closer to the camera.';
    if (m.box.w > 0.72) return 'Move back a little.';
    if (Math.abs(m.box.cx - 0.5) > 0.2 || Math.abs(m.box.cy - 0.5) > 0.24) return 'Centre your face in the oval.';
    if (Math.abs(m.turn) > 0.12) return 'Look straight at the camera.';
    return '';
  }

  async _until(name, timeoutMs, step) {
    const deadline = performance.now() + timeoutMs;
    while (performance.now() < deadline) {
      const m = await this._frame();
      if (m.faces !== 1) { this.warn(m.faces === 0 ? 'We can’t see your face.' : 'Only you should be in view.'); continue; }
      this.warn('');
      if (await step(m)) return;
    }
    throw new TaskTimeout(name);
  }

  async _center() {
    let stableSince = 0, bright = 0, ratio = 0, n = 0;
    await this._until('center', 30000, (m) => {
      const problem = FaceVerifier.framingProblem(m);
      if (problem) { this.warn(problem); stableSince = 0; return false; }
      if (!stableSince) stableSince = performance.now();
      bright += m.brightness; ratio += m.box.w; n++;
      return performance.now() - stableSince >= 1300;
    });
    return { brightness: Math.round(bright / n), face_ratio: +(ratio / n).toFixed(3) };
  }

  _blink() {
    let open = 0, closed = false;
    return this._until('blink', 15000, (m) => {
      if (!closed) { if (m.blink < 0.25) open++; else if (open >= 3 && m.blink > 0.45) closed = true; return false; }
      return m.blink < 0.3;
    });
  }

  _smile() {
    let neutral = false, hits = 0;
    return this._until('smile', 15000, (m) => {
      if (m.smile < 0.25) neutral = true;
      hits = neutral && m.smile > 0.42 ? hits + 1 : 0;
      return hits >= 3;
    });
  }

  _openMouth() {
    let neutral = false, hits = 0;
    return this._until('open_mouth', 15000, (m) => {
      if (m.jaw < 0.14) neutral = true;
      hits = neutral && m.jaw > 0.35 ? hits + 1 : 0;
      return hits >= 3;
    });
  }

  _turnHead() {
    let first = 0, done = false;
    return this._until('turn_head', 20000, (m) => {
      const side = m.turn > 0.16 ? 1 : m.turn < -0.16 ? -1 : 0;
      if (!first && side) first = side;
      else if (first && side === -first) done = true;
      return done && Math.abs(m.turn) < 0.1;
    });
  }

  async capture() {
    const v = this.video, canvas = document.createElement('canvas');
    const scale = Math.min(1, 720 / v.videoWidth);
    canvas.width = Math.round(v.videoWidth * scale); canvas.height = Math.round(v.videoHeight * scale);
    canvas.getContext('2d').drawImage(v, 0, 0, canvas.width, canvas.height);     // un-mirrored, as the camera sees it
    return new Promise((res) => canvas.toBlob(res, 'image/jpeg', 0.88));
  }

  // Runs the challenge the server issued. Returns {steps, photo, metrics}.
  async run(tasks) {
    const steps = []; let photo = null; let metrics = {};
    const t0 = performance.now();
    for (const name of tasks) {
      let attempts = 0;
      for (;;) {
        this.onTask(name, 'start');
        this.say(TASK_HELP[name]);
        try {
          if (name === 'center') { metrics = await this._center(); photo = await this.capture(); }
          else if (name === 'blink') await this._blink();
          else if (name === 'smile') await this._smile();
          else if (name === 'open_mouth') await this._openMouth();
          else if (name === 'turn_head') await this._turnHead();
          steps.push({ name, passed: true, ms: Math.round(performance.now() - t0) });
          this.onTask(name, 'done');
          break;
        } catch (e) {
          if (!(e instanceof TaskTimeout)) throw e;
          if (++attempts >= 3) throw new Error('We couldn’t complete the face check. Please check your lighting and camera, then try again.');
          this.warn('Let’s try that once more.');
        }
      }
    }
    this.warn('');
    return { steps, photo, metrics };
  }
}

export class PresenceMonitor {
  constructor({ engine, video, startedAt, onEvent, onState }) {
    this.engine = engine; this.video = video; this.startedAt = startedAt;
    this.onEvent = onEvent; this.onState = onState || (() => {});
    this.timer = null; this.cur = { face_absent: 0, multiple_faces: 0, looking_away: 0 };
  }
  start() { this.timer = setInterval(() => this._tick(), 600); }
  stop() {
    clearInterval(this.timer); this.timer = null;
    const now = performance.now();
    for (const k of Object.keys(this.cur)) this._end(k, now);
  }
  _end(kind, now) {
    const since = this.cur[kind];
    if (!since) return;
    this.cur[kind] = 0;
    const dur = now - since;
    const min = kind === 'face_absent' ? 3000 : kind === 'multiple_faces' ? 2000 : 5000;
    if (dur >= min) this.onEvent({ type: kind, t: Math.round(Date.now() - this.startedAt - dur), duration_ms: Math.round(dur) });
  }
  _tick() {
    const m = this.engine.analyze(this.video);
    const now = performance.now();
    const flags = {
      face_absent: m.faces === 0,
      multiple_faces: m.faces > 1,
      looking_away: m.faces === 1 && Math.abs(m.turn) > 0.4,
    };
    for (const [k, on] of Object.entries(flags)) {
      if (on && !this.cur[k]) this.cur[k] = now;
      else if (!on && this.cur[k]) this._end(k, now);
    }
    const absent = this.cur.face_absent && now - this.cur.face_absent > 3000;
    const multi = this.cur.multiple_faces && now - this.cur.multiple_faces > 2000;
    this.onState(absent ? 'We can’t see you — please stay in front of the camera.' : multi ? 'Please make sure you are alone for this interview.' : '');
  }
}
