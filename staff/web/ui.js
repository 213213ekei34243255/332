// Tiny DOM + fetch helpers shared by the staff pages. No innerHTML with data -> no XSS from applicant input.
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === false || v === null || v === undefined) continue;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'hidden' || k === 'required' || k === 'multiple') el[k] = !!v;
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export function mount(el, ...children) { clear(el); append(el, children); return el; }
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function fmtDate(ms, withTime = true) {
  if (!ms) return '—';
  const d = new Date(Number(ms));
  const opts = withTime ? { dateStyle: 'medium', timeStyle: 'short' } : { dateStyle: 'medium' };
  return d.toLocaleString(undefined, opts);
}
export function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  const m = Math.floor(seconds / 60), s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
export function ago(ms) {
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

let toastBox;
export function toast(message, kind = '') {
  if (!toastBox) { toastBox = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(toastBox); }
  const t = h('div', { class: `toast ${kind}` }, message);
  toastBox.append(t);
  setTimeout(() => t.remove(), kind === 'error' ? 7000 : 3800);
}

export class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data || {}; }
}

export async function request(url, { method = 'GET', json, form, body, headers = {}, token, csrf, signal, timeout = 60000 } = {}) {
  const opts = { method, headers: { ...headers }, credentials: 'same-origin', signal };
  if (token) opts.headers.Authorization = `Bearer ${token}`;
  if (csrf && method !== 'GET') opts.headers['X-CSRF-Token'] = csrf;
  if (json !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(json); }
  else if (form) opts.body = form;
  else if (body !== undefined) opts.body = body;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  if (signal) signal.addEventListener('abort', () => ctl.abort());
  opts.signal = ctl.signal;
  let res;
  try { res = await fetch(url, opts); }
  catch (e) { throw new ApiError(e.name === 'AbortError' ? 'The request timed out.' : 'Network problem. Check your connection.', 0, {}); }
  finally { clearTimeout(timer); }
  const type = res.headers.get('content-type') || '';
  if (type.includes('audio') || type.includes('video')) {
    if (!res.ok) throw new ApiError('Request failed', res.status, {});
    return res;
  }
  let data = {};
  try { data = await res.json(); } catch { /* not json */ }
  if (!res.ok || data.ok === false) throw new ApiError(data.error || `Request failed (${res.status})`, res.status, data);
  return data;
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
