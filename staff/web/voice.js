// Hands-free voice I/O for the interview room.
//
//  * SpeechOut  - natural server TTS (Rexy Voice) played through WebAudio so it is mixed into the recording,
//                 with automatic fallback to the browser's speechSynthesis (sentence-chunked, voice-ranked).
//  * SpeechIn   - voice-activity detection decides when the candidate has finished; Web Speech API gives
//                 live captions; the audio segment is re-transcribed by the Rexy model when the browser engine
//                 fails, is unsupported (Firefox) or returned too little for the amount of speech.
//
// The interview is strictly half-duplex: the mic is not "listening" while the interviewer speaks, which is
// what removes the echo / self-interruption / double-answer problems of the old always-on recognizer.

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- pure helpers (unit-tested in Node)
export function splitSentences(text, maxLen = 200) {
  const clean = String(text || '').replace(/\s+/g, ' ').trim();
  if (!clean) return [];
  const raw = clean.match(/[^.!?…।॥]+(?:[.!?…।॥]+["')\]]*|$)/g) || [clean];     // । ॥ end Hindi sentences
  const out = [];
  for (let part of raw) {
    part = part.trim();
    if (!part) continue;
    while (part.length > maxLen) {           // very long sentence: break at a comma/space
      let cut = part.lastIndexOf(',', maxLen);
      if (cut < maxLen * 0.4) cut = part.lastIndexOf(' ', maxLen);
      if (cut < 1) cut = maxLen;
      out.push(part.slice(0, cut + 1).trim());
      part = part.slice(cut + 1).trim();
    }
    if (part) out.push(part);
  }
  return out;
}

// Group sentences into TTS requests: the first request is a single sentence (fast time-to-first-audio),
// later ones are merged up to ~220 chars so we make fewer round trips.
export function groupForTts(sentences, maxLen = 220) {
  const groups = [];
  sentences.forEach((s, i) => {
    if (i === 0) { groups.push(s); return; }
    const last = groups[groups.length - 1];
    if (groups.length > 1 && (last + ' ' + s).length <= maxLen) groups[groups.length - 1] = last + ' ' + s;
    else groups.push(s);
  });
  return groups;
}

const CONTINUATION = /\b(and|but|so|because|like|um+|uh+|er+|the|a|an|to|of|with|for|or|that|which|my|is|was|if|when|then|also)\s*$/i;
export function endpointMs(text, vadOnly = false) {
  const t = String(text || '').trim();
  const words = t ? t.split(/\s+/).length : 0;
  let ms = vadOnly ? 1800 : 1500;
  if (words > 0 && words < 4) ms = 2200;
  if (words > 60) ms = 1200;
  else if (CONTINUATION.test(t)) ms = Math.min(3600, ms + 1100);
  return ms;
}

export function wordCount(text) { const t = String(text || '').trim(); return t ? t.split(/\s+/).length : 0; }

export function joinTranscript(committed, fin, interim) {
  return [committed, fin, interim].map((x) => (x || '').trim()).filter(Boolean).join(' ').replace(/\s+/g, ' ');
}

export function dedupeResults(texts) {          // Android Chrome re-emits finals in continuous mode
  const out = [];
  for (const t of texts) if (!out.length || out[out.length - 1].trim().toLowerCase() !== t.trim().toLowerCase()) out.push(t);
  return out;
}

export function pickAudioMime() {
  if (typeof MediaRecorder === 'undefined') return '';
  for (const m of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus']) {
    if (MediaRecorder.isTypeSupported(m)) return m;
  }
  return '';
}

// ---------------------------------------------------------------- audio graph
export class AudioHub {
  constructor() {
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC({ latencyHint: 'interactive' });
    this.recordDest = this.ctx.createMediaStreamDestination();   // mic + interviewer voice -> recording
    this.out = this.ctx.createGain();
    this.out.connect(this.ctx.destination);
    this.out.connect(this.recordDest);
    this.outAnalyser = this.ctx.createAnalyser();
    this.outAnalyser.fftSize = 512;
    this.out.connect(this.outAnalyser);
    this.micSource = null; this.micAnalyser = null;
    this._buf = new Float32Array(1024);
    this._out = new Float32Array(512);
  }
  async resume() { if (this.ctx.state !== 'running') { try { await this.ctx.resume(); } catch { /* needs a gesture */ } } }
  attachMic(stream) {
    this.detachMic();
    this.micSource = this.ctx.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
    this.micSource.connect(this.recordDest);
    this.micAnalyser = this.ctx.createAnalyser();
    this.micAnalyser.fftSize = 1024;
    this.micSource.connect(this.micAnalyser);
  }
  detachMic() { try { this.micSource?.disconnect(); } catch { /* */ } this.micSource = null; this.micAnalyser = null; }
  static rms(analyser, buf) {
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    return Math.sqrt(sum / buf.length);
  }
  micLevel() { return this.micAnalyser ? AudioHub.rms(this.micAnalyser, this._buf) : 0; }
  outLevel() { return AudioHub.rms(this.outAnalyser, this._out); }
  close() { this.detachMic(); try { this.ctx.close(); } catch { /* */ } }
}

// ---------------------------------------------------------------- voice activity
export class VoiceActivity {
  constructor(hub) {
    this.hub = hub; this.timer = null; this.reset();
  }
  reset() {
    this.floor = this.floor || 0.008; this.run = 0; this.speaking = false; this.onsetAt = 0; this.lastVoiceAt = 0; this.level = 0;
  }
  start() {
    if (this.timer) return;
    this.timer = setInterval(() => this._tick(), 40);
  }
  stop() { clearInterval(this.timer); this.timer = null; }
  _tick() {
    const rms = this.hub.micLevel();
    this.level = rms;
    if (rms < this.floor) this.floor = Math.max(0.002, rms); else this.floor += (rms - this.floor) * 0.002;
    this.floor = Math.min(this.floor, 0.03);
    const thr = Math.max(0.011, this.floor * 3);
    const now = performance.now();
    if (rms > thr) {
      this.run++;
      this.lastVoiceAt = now;
      if (this.run >= 3 && !this.speaking) { this.speaking = true; this.onsetAt = now - 120; }
    } else {
      this.run = 0;
      if (this.speaking && now - this.lastVoiceAt > 250) this.speaking = false;
    }
  }
}

// ---------------------------------------------------------------- text to speech
export class SpeechOut extends EventTarget {
  constructor({ hub, fetchTts = null, lang = 'en-IN' }) {
    super();
    this.hub = hub; this.fetchTts = fetchTts; this.lang = lang;
    this.mode = fetchTts ? 'server' : 'browser';
    this.failures = 0; this.speaking = false; this.cancelled = false;
    this._src = null; this._browserEnd = null; this._voice = undefined;
  }

  async speak(text) {
    const sentences = splitSentences(text);
    if (!sentences.length) return;
    this.cancelled = false; this.speaking = true;
    this.dispatchEvent(new Event('start'));
    try {
      let rest = sentences;
      if (this.mode === 'server') {
        const groups = groupForTts(sentences);
        const failedAt = await this._speakServer(groups);
        if (failedAt === -1 || this.cancelled) return;
        rest = groups.slice(failedAt);
      }
      await this._speakBrowser(rest);
    } finally {
      this.speaking = false;
      this.dispatchEvent(new Event('end'));
    }
  }

  setLang(lang) { if (lang && lang !== this.lang) { this.lang = lang; this._voice = undefined; } }   // re-rank browser voices

  stop() {
    this.cancelled = true;
    try { this._src?.stop(); } catch { /* already ended */ }
    try { window.speechSynthesis?.cancel(); } catch { /* */ }
    this._browserEnd?.();
  }

  // Returns -1 when everything was spoken, otherwise the index of the first group that failed.
  async _speakServer(groups) {
    const decoded = new Map();
    const get = (i) => {
      if (!decoded.has(i)) {
        decoded.set(i, this.fetchTts(groups[i]).then((buf) => this.hub.ctx.decodeAudioData(buf)));
        decoded.get(i).catch(() => {});            // avoid unhandled-rejection noise for prefetches
      }
      return decoded.get(i);
    };
    get(0); if (groups.length > 1) get(1);
    for (let i = 0; i < groups.length; i++) {
      if (this.cancelled) return -1;
      let audio;
      try { audio = await get(i); this.failures = 0; }
      catch (e) {
        this.failures++;
        if (this.failures >= 2 || (e && (e.status === 501 || e.status === 401))) this.mode = 'browser';
        return i;
      }
      if (i + 2 < groups.length) get(i + 2);
      if (this.cancelled) return -1;
      await this._play(audio);
    }
    return -1;
  }

  _play(buffer) {
    return new Promise((resolve) => {
      const src = this.hub.ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(this.hub.out);
      this._src = src;
      src.onended = () => { this._src = null; resolve(); };
      src.start();
    });
  }

  async _voices() {
    const synth = window.speechSynthesis;
    if (!synth) return [];
    let list = synth.getVoices();
    if (!list.length) {
      await new Promise((res) => { const done = () => res(); synth.addEventListener('voiceschanged', done, { once: true }); setTimeout(done, 1500); });
      list = synth.getVoices();
    }
    return list;
  }

  async _pickVoice() {
    if (this._voice !== undefined) return this._voice;
    const list = await this._voices();
    const want = this.lang.toLowerCase(); const base = want.split('-')[0];
    let best = null, bestScore = -1;
    for (const v of list) {
      const l = v.lang.toLowerCase().replace('_', '-');
      if (!l.startsWith(base)) continue;
      let s = 1;
      if (l === want) s += 4;
      else if (['en-in', 'en-gb', 'en-us', 'en-au'].includes(l)) s += 2;
      if (/natural|neural/i.test(v.name)) s += 4;
      if (/online|google|siri|premium|enhanced/i.test(v.name)) s += 2;
      if (/microsoft|samantha|daniel|karen|moira|veena|rishi/i.test(v.name)) s += 1;
      if (/espeak|compact/i.test(v.name)) s -= 3;
      if (s > bestScore) { best = v; bestScore = s; }
    }
    this._voice = best;
    return best;
  }

  async _speakBrowser(parts) {
    const synth = window.speechSynthesis;
    if (!synth) return;
    const voice = await this._pickVoice();
    const sentences = parts.flatMap((p) => splitSentences(p, 160));
    synth.cancel();
    await sleep(60);                                   // Chrome drops speak() issued right after cancel()
    for (const s of sentences) {
      if (this.cancelled) break;
      await new Promise((resolve) => {
        const u = new SpeechSynthesisUtterance(s);
        if (voice) { u.voice = voice; u.lang = voice.lang; } else u.lang = this.lang;
        u.rate = 1; u.pitch = 1;
        let finished = false;
        const end = () => { if (finished) return; finished = true; clearTimeout(watchdog); this._browserEnd = null; resolve(); };
        const watchdog = setTimeout(end, Math.max(3500, s.length * 120 + 2500));   // Chrome sometimes never fires onend
        u.onend = end; u.onerror = end;
        this._browserEnd = end;
        synth.speak(u);
      });
    }
  }
}

// ---------------------------------------------------------------- speech to text
export class SpeechIn extends EventTarget {
  constructor({ hub, stream, lang = 'en-IN', transcribe = null, preferServer = false }) {
    super();
    this.hub = hub; this.stream = stream; this.lang = lang; this.transcribe = transcribe; this.preferServer = preferServer;
    this.vad = new VoiceActivity(hub);
    this.srBroken = !(window.SpeechRecognition || window.webkitSpeechRecognition);
    this.srNetErrors = 0;
    this._force = null;
    this.vad.start();
  }

  setLang(lang) { if (lang) this.lang = lang; }       // read each time a listening turn starts
  get usingBrowserEngine() { return !this.srBroken; }
  get level() { return this.vad.level; }
  forceDone() { this._force?.('forced'); }
  cancel() { this._force?.('cancelled'); }
  destroy() { this.cancel(); this.vad.stop(); }

  listen({ maxMs = 120000, noSpeechMs = 22000 } = {}) {
    return new Promise((resolve) => {
      this.vad.reset();
      const t0 = performance.now();
      let heard = false, speechStart = 0, lastActivity = 0, finished = false;
      let committed = '', fin = '', interim = '', lastText = '';
      const sr = this._startRecognition({
        onResult: (f, i) => { fin = f; interim = i; },
        onRestart: () => { committed = joinTranscript(committed, fin, interim); fin = ''; interim = ''; },
      });
      const segment = this._startSegment();

      const finish = async (reason) => {
        if (finished) return;
        finished = true; clearInterval(timer); this._force = null;
        sr?.stop();
        const blob = await segment.stop();
        if (reason === 'cancelled') return resolve({ text: '', source: 'none', reason, speechMs: 0, delayMs: null });
        const speechMs = heard ? Math.max(0, performance.now() - speechStart) : 0;
        let text = joinTranscript(committed, fin, interim);
        let source = text ? 'browser' : 'none';
        const suspicious = wordCount(text) <= 2 && speechMs > 4000;
        if (this.transcribe && blob && blob.size > 1500 && heard && (!text || suspicious || this.preferServer)) {
          try {
            const remote = await this.transcribe(blob);
            if (remote && (!text || suspicious || this.preferServer || remote.length >= text.length * 0.8)) { text = remote; source = 'server'; }
          } catch { /* keep whatever the browser produced */ }
        }
        resolve({ text: text.trim(), source, speechMs, reason, delayMs: heard ? Math.max(0, speechStart - t0) : null });
      };
      this._force = finish;

      const timer = setInterval(() => {
        const now = performance.now();
        const current = joinTranscript(committed, fin, interim);
        if (current !== lastText) { lastText = current; lastActivity = now; this.dispatchEvent(new CustomEvent('interim', { detail: current })); }
        if (this.vad.speaking) lastActivity = now;
        if (!heard && this.vad.onsetAt > t0) { heard = true; speechStart = this.vad.onsetAt; this.dispatchEvent(new Event('speechstart')); }
        if (!heard && current) { heard = true; speechStart = now; this.dispatchEvent(new Event('speechstart')); }
        if (!heard && now - t0 > noSpeechMs) return finish('silence');
        if (heard && now - lastActivity >= endpointMs(current, !sr)) return finish('done');
        if (now - t0 > maxMs) finish('max');
      }, 100);
    });
  }

  _startRecognition({ onResult, onRestart }) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR || this.srBroken) return null;
    const rec = new SR();
    rec.lang = this.lang; rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1;
    let stopped = false; let restarts = [];
    rec.onresult = (e) => {
      const finals = [], interims = [];
      for (let i = 0; i < e.results.length; i++) (e.results[i].isFinal ? finals : interims).push(e.results[i][0].transcript);
      onResult(dedupeResults(finals).join(' '), interims.join(' '));
    };
    rec.onerror = (e) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed' || e.error === 'language-not-supported') { this.srBroken = true; stopped = true; }
      else if (e.error === 'network') { if (++this.srNetErrors >= 2) this.srBroken = true; }
    };
    rec.onend = () => {
      if (stopped) return;
      const now = Date.now();
      restarts = restarts.filter((t) => now - t < 4000); restarts.push(now);
      if (restarts.length > 5 || this.srBroken) { this.srBroken = true; return; }
      onRestart();
      setTimeout(() => { if (!stopped) { try { rec.start(); } catch { /* already started */ } } }, 120);
    };
    try { rec.start(); } catch { this.srBroken = true; return null; }
    return { stop() { stopped = true; try { rec.stop(); } catch { /* */ } } };
  }

  _startSegment() {
    const mime = pickAudioMime();
    if (!this.stream || !mime) return { stop: async () => null };
    let rec;
    try { rec = new MediaRecorder(new MediaStream(this.stream.getAudioTracks()), { mimeType: mime, audioBitsPerSecond: 32000 }); }
    catch { return { stop: async () => null }; }
    const chunks = [];
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.start(500);
    return {
      stop: () => new Promise((resolve) => {
        if (rec.state === 'inactive') return resolve(chunks.length ? new Blob(chunks, { type: mime }) : null);
        rec.onstop = () => resolve(chunks.length ? new Blob(chunks, { type: mime }) : null);
        try { rec.stop(); } catch { resolve(null); }
      }),
    };
  }
}
