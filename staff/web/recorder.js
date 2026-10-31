// Records the interview (candidate video + burned-in captions + candidate mic + interviewer voice) and
// streams it to the server in small ordered chunks, so a dropped connection never loses the whole video
// and nothing large is ever uploaded in one request.
import { sleep } from './voice.js';

const VIDEO_MIMES = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4'];

export function pickVideoMime() {
  if (typeof MediaRecorder === 'undefined') return '';
  return VIDEO_MIMES.find((m) => MediaRecorder.isTypeSupported(m)) || '';
}

export class UploadQueue {
  constructor({ upload, segment }) {
    this.upload = upload;            // async (segment, seq, blob) => void ; throws {status, data}
    this.segment = segment; this.seq = 0; this.pending = []; this.sentBytes = 0; this.busy = false;
    this.failed = false; this.lastError = ''; this.onStatus = () => {};
  }
  push(blob) {
    this.pending.push({ seq: this.seq++, blob });
    this._pump();
  }
  get chunks() { return this.seq; }
  async _pump() {
    if (this.busy) return;
    this.busy = true;
    let delay = 800;
    while (this.pending.length) {
      const item = this.pending[0];
      try {
        await this.upload(this.segment, item.seq, item.blob);
        this.pending.shift(); this.sentBytes += item.blob.size; delay = 800; this.lastError = '';
      } catch (e) {
        if (e.status === 409 && e.data && e.data.expected_seq !== undefined) {
          if (e.data.expected_seq > item.seq) { this.pending.shift(); continue; }      // server already has it
          this.failed = true; this.lastError = 'The server lost part of the recording.'; break;
        }
        if (e.status === 401 || e.status === 403 || e.status === 507 || e.status === 413) { this.failed = true; this.lastError = e.message; break; }
        this.lastError = e.message || 'upload error';
        await sleep(delay); delay = Math.min(delay * 2, 15000);
      }
      this.onStatus(this);
    }
    this.busy = false;
    this.onStatus(this);
  }
  async drain(timeoutMs = 90000) {
    const end = Date.now() + timeoutMs;
    while ((this.pending.length || this.busy) && Date.now() < end && !this.failed) await sleep(250);
    return !this.pending.length && !this.failed;
  }
}

export class InterviewRecorder {
  constructor({ video, hub, getOverlay, label, videoBps = 600000, audioBps = 64000, onChunk, onError }) {
    this.video = video; this.hub = hub; this.getOverlay = getOverlay; this.label = label;
    this.videoBps = videoBps; this.audioBps = audioBps; this.onChunk = onChunk; this.onError = onError || (() => {});
    this.canvas = document.createElement('canvas');
    this.canvas.width = 640; this.canvas.height = 360;
    this.ctx2d = this.canvas.getContext('2d', { alpha: false });
    this.recorder = null; this.timer = null; this.startedAt = 0; this.mime = '';
  }

  start() {
    this.mime = pickVideoMime();
    if (!this.mime) throw new Error('This browser cannot record video (MediaRecorder is unavailable). Please use the latest Chrome, Edge or Safari.');
    this.startedAt = Date.now();
    this._draw();
    this.timer = setInterval(() => this._draw(), 1000 / 15);
    const videoTrack = this.canvas.captureStream(15).getVideoTracks()[0];
    const stream = new MediaStream([videoTrack, ...this.hub.recordDest.stream.getAudioTracks()]);
    this.recorder = new MediaRecorder(stream, { mimeType: this.mime, videoBitsPerSecond: this.videoBps, audioBitsPerSecond: this.audioBps });
    this.recorder.ondataavailable = (e) => { if (e.data && e.data.size) this.onChunk(e.data); };
    this.recorder.onerror = (e) => this.onError(e.error || new Error('Recorder error'));
    this.recorder.start(4000);
  }

  get durationMs() { return Date.now() - this.startedAt; }
  get baseMime() { return this.mime.split(';')[0]; }

  _draw() {
    const { ctx2d: c, canvas, video } = this;
    const w = canvas.width, h = canvas.height;
    if (video.readyState >= 2 && video.videoWidth) {
      const vr = video.videoWidth / video.videoHeight, cr = w / h;
      let sw = video.videoWidth, sh = video.videoHeight, sx = 0, sy = 0;
      if (vr > cr) { sw = sh * cr; sx = (video.videoWidth - sw) / 2; } else { sh = sw / cr; sy = (video.videoHeight - sh) / 2; }
      c.drawImage(video, sx, sy, sw, sh, 0, 0, w, h);
    } else { c.fillStyle = '#0d1420'; c.fillRect(0, 0, w, h); }
    // header: who/where/when, useful for reviewers scrubbing the file
    c.fillStyle = 'rgba(8,14,26,.62)'; c.fillRect(0, 0, w, 24);
    c.fillStyle = '#e6ebf5'; c.font = '600 12px system-ui, sans-serif'; c.textBaseline = 'middle';
    const secs = Math.floor((Date.now() - this.startedAt) / 1000);
    c.fillText(`${this.label}  •  ${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`, 10, 12);
    c.fillStyle = '#ff5a52'; c.beginPath(); c.arc(w - 14, 12, 4.5, 0, 7); c.fill();
    const o = this.getOverlay && this.getOverlay();
    if (o && o.text) {
      c.font = '500 15px system-ui, sans-serif';
      const lines = this._wrap(o.text, w - 56).slice(-3);
      const bh = lines.length * 21 + (o.who ? 26 : 14);
      const by = h - bh - 12;
      c.fillStyle = 'rgba(8,14,26,.74)'; c.fillRect(16, by, w - 32, bh);
      let y = by + 10;
      if (o.who) { c.fillStyle = '#6fb1ff'; c.font = '700 11px system-ui, sans-serif'; c.fillText(o.who.toUpperCase(), 28, y + 6); y += 18; }
      c.fillStyle = '#fff'; c.font = '500 15px system-ui, sans-serif';
      lines.forEach((l) => { c.fillText(l, 28, y + 9); y += 21; });
    }
  }

  _wrap(text, maxW) {
    const words = String(text).split(/\s+/); const lines = []; let line = '';
    for (const wd of words) {
      const t = line ? `${line} ${wd}` : wd;
      if (this.ctx2d.measureText(t).width > maxW && line) { lines.push(line); line = wd; } else line = t;
    }
    if (line) lines.push(line);
    return lines;
  }

  stop() {
    return new Promise((resolve) => {
      clearInterval(this.timer);
      const r = this.recorder;
      if (!r || r.state === 'inactive') return resolve();
      r.onstop = () => resolve();
      try { r.requestData(); } catch { /* */ }
      try { r.stop(); } catch { resolve(); }
    });
  }
}
