import { $, $$, h, mount, request, toast, fmtDate, fmtDuration, ago, ApiError, sleep } from './ui.js';

const A = { csrf: null, org: 'Staff', system: null, positions: [], poll: null };
const call = (path, opts = {}) => request(`/staff/api/${path}`, { csrf: A.csrf, ...opts });
const view = $('#view');

// ------------------------------------------------------------------ labels
const APP_STATUS = { submitted: ['New', 'info'], needs_review: ['Needs review', 'warn'], invited: ['Invited', 'info'], face_verified: ['Face verified', 'info'],
  interviewing: ['Interviewing', 'warn'], interviewed: ['Interviewed', 'info'], evaluated: ['Evaluated', 'ok'] };
const ENR_STATUS = { issued: ['Code sent', 'info'], opened: ['Code opened', 'info'], verified: ['Face verified', 'info'], in_progress: ['In progress', 'warn'],
  completed: ['Completed', 'ok'], expired: ['Expired', 'danger'], revoked: ['Revoked', 'danger'] };
const IV_STATUS = { in_progress: ['In progress', 'warn'], evaluating: ['AI evaluating…', 'info'], evaluated: ['Evaluated', 'ok'], evaluation_failed: ['Evaluation failed', 'danger'] };
const DECISION = { shortlisted: ['Shortlisted', 'ok'], on_hold: ['On hold', 'warn'], rejected: ['Rejected', 'danger'], hired: ['Hired', 'ok'] };
const RECO = { strong_yes: ['Strong yes', 'ok'], yes: ['Yes', 'ok'], maybe: ['Maybe', 'warn'], no: ['No', 'danger'], strong_no: ['Strong no', 'danger'], no_data: ['Not enough data', ''] };
const EDU = { '': 'Any', high_school: 'High school', diploma: 'Diploma', bachelor: "Bachelor's", master: "Master's", doctorate: 'Doctorate' };

const badge = (map, key, fallback = '—') => (map[key] ? h('span', { class: `badge ${map[key][1]}` }, map[key][0]) : h('span', { class: 'badge' }, key || fallback));
const scoreKind = (s) => (s >= 70 ? 'ok' : s >= 50 ? 'warn' : 'danger');

// ------------------------------------------------------------------ small UI helpers
function modal(title, body, actions = []) {
  const close = () => back.remove();
  const back = h('div', { class: 'modal-back', onclick: (e) => { if (e.target === back) close(); } },
    h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      h('h2', null, title), body,
      h('div', { class: 'row', style: null }, h('span', { class: 'grow' }), actions.map((a) => h('button', { class: `btn ${a.kind || ''}`, onclick: async () => { const keep = await a.onClick?.(close); if (keep !== false) close(); } }, a.label)))));
  document.body.append(back);
  return close;
}
const confirmDialog = (title, text, label = 'Confirm', kind = 'danger') => new Promise((resolve) => {
  modal(title, h('p', { class: 'muted' }, text), [{ label: 'Cancel', kind: 'secondary', onClick: () => resolve(false) }, { label, kind, onClick: () => resolve(true) }]);
});
const field = (label, input, hint) => h('div', null, h('label', { class: 'field' }, label), input, hint && h('div', { class: 'tiny muted' }, hint));
const empty = (msg) => h('div', { class: 'empty' }, msg);
async function copy(text, msg = 'Copied') { try { await navigator.clipboard.writeText(text); toast(msg, 'ok'); } catch { toast('Copy failed — select and copy manually', 'error'); } }
const spinner = () => h('div', { class: 'empty' }, h('span', { class: 'spinner' }));
function guard(fn) { return async (...args) => { try { return await fn(...args); } catch (e) { if (e.status === 401) return showLogin(); toast(e.message, 'error'); } }; }
function pagerEl(data, go) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  return h('div', { class: 'pager' }, h('span', null, `${data.total} result${data.total === 1 ? '' : 's'}`),
    h('div', { class: 'row' }, h('button', { class: 'btn secondary small', disabled: data.page <= 1, onclick: () => go(data.page - 1) }, '← Prev'),
      h('span', null, `Page ${data.page} / ${pages}`), h('button', { class: 'btn secondary small', disabled: data.page >= pages, onclick: () => go(data.page + 1) }, 'Next →')));
}
const link = (hash, text) => h('a', { href: hash }, text);

// Decisions that email the applicant: passed the first round (shortlisted / hired) or rejected.
// The admin always sees and can edit the message first, and can save without sending.
const EMAILED = ['shortlisted', 'hired', 'rejected'];
async function setDecision(appId, current, d, done) {
  if (current === d || !EMAILED.includes(d)) {         // clearing a decision, or "on hold": nothing to email
    await call(`applications/${appId}`, { method: 'PATCH', json: { decision: current === d ? '' : d } });
    toast(current === d ? 'Decision cleared' : `Marked ${DECISION[d][0].toLowerCase()}`, 'ok');
    return done();
  }
  const prev = (await call(`applications/${appId}/decision-email?decision=${d}`)).email;
  const subject = h('input', { type: 'text', value: prev.subject, maxlength: 200 });
  const message = h('textarea', { rows: 12, maxlength: 3000 }, prev.message);
  const send = h('input', { type: 'checkbox', checked: true });
  modal(`Mark as ${DECISION[d][0].toLowerCase()}`, h('div', { class: 'stack' },
    prev.earlier.length > 0 && h('div', { class: 'notice warn' }, `A decision email was already sent to this applicant (${prev.earlier.map((e) => `${e.kind.replace('decision_', '')}, ${e.status}`).join('; ')}).`),
    h('p', { class: 'small muted' }, 'The email goes to ', h('strong', null, prev.to), '. The greeting and sign-off are added automatically.'),
    field('Subject', subject), field('Message', message),
    h('label', { class: 'check' }, send, h('span', null, 'Email this to the applicant when I save'))),
  [{ label: 'Cancel', kind: 'secondary' }, { label: 'Save decision', onClick: async () => {
    try {
      const r = await call(`applications/${appId}`, { method: 'PATCH', json: { decision: d, notify: send.checked, subject: subject.value, message: message.value } });
      toast(r.email?.id ? 'Decision saved and email queued' : r.email?.skipped === 'already_sent' ? 'Decision saved (that email was already sent earlier)' : 'Decision saved', 'ok');
      done();
    } catch (e) { toast(e.message, 'error'); return false; }
  } }]);
}

// ------------------------------------------------------------------ session
function showLogin(msg) {
  $('#app').hidden = true; $('#login').hidden = false; $('#login-org').textContent = A.org;
  const err = $('#login-error'); err.hidden = !msg; err.textContent = msg || '';
  $('#password').focus();
}
$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#login-btn'); btn.disabled = true;
  try {
    const out = await request('/staff/api/login', { method: 'POST', json: { password: $('#password').value } });
    A.csrf = out.csrf; $('#password').value = ''; boot();
  } catch (er) { showLogin(er.message); } finally { btn.disabled = false; }
});
$('#logout-btn').addEventListener('click', async () => { await request('/staff/api/logout', { method: 'POST' }).catch(() => {}); A.csrf = null; showLogin(); });
$('#menu-btn').addEventListener('click', () => $('#side').classList.toggle('open'));

async function init() {
  const me = await request('/staff/api/me');
  A.org = me.org || 'Staff';
  if (!me.configured) { showLogin('Admin access is not configured. Set STAFF_ADMIN_PASSWORD (or ADMIN_API_KEY) on the server.'); return; }
  if (me.signed_in) { A.csrf = me.csrf; boot(); } else showLogin();
}
function boot() {
  $('#login').hidden = true; $('#app').hidden = false; $('#org').textContent = A.org; document.title = `${A.org} · Staff dashboard`;
  window.addEventListener('hashchange', route);
  route();
}

// ------------------------------------------------------------------ router
const routes = [
  [/^#\/overview$/, 'overview', viewOverview], [/^#\/positions$/, 'positions', viewPositions],
  [/^#\/positions\/new$/, 'positions', () => viewPositionEditor(null)], [/^#\/positions\/([\w-]+)$/, 'positions', (id) => viewPositionEditor(id)],
  [/^#\/applications$/, 'applications', () => viewApplications()], [/^#\/applications\/([\w-]+)$/, 'applications', viewApplication],
  [/^#\/interviews$/, 'interviews', () => viewInterviews()], [/^#\/interviews\/([\w-]+)$/, 'interviews', viewInterview],
  [/^#\/emails$/, 'emails', viewEmails], [/^#\/system$/, 'system', viewSystem],
];
async function route() {
  clearInterval(A.poll);
  $('#side').classList.remove('open');
  const hash = location.hash || '#/overview';
  for (const [re, nav, fn] of routes) {
    const m = hash.match(re);
    if (!m) continue;
    $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === nav));
    $('#mobile-title').textContent = nav[0].toUpperCase() + nav.slice(1);
    mount(view, spinner());
    try { await fn(...m.slice(1)); } catch (e) { if (e.status === 401) return showLogin('Your session expired. Please sign in again.'); mount(view, h('div', { class: 'notice danger' }, e.message)); }
    view.focus({ preventScroll: true });
    return;
  }
  location.hash = '#/overview';
}

async function loadPositions() { A.positions = (await call('positions')).items; return A.positions; }
const positionSelect = (value, onchange, label = 'All positions') => h('select', { onchange: (e) => onchange(e.target.value) },
  h('option', { value: '' }, label), A.positions.map((p) => h('option', { value: p.id, selected: p.id === value }, p.title)));

// ------------------------------------------------------------------ overview
async function viewOverview() {
  const [{ stats }, { system }, apps, ivs] = await Promise.all([call('stats'), call('system'), call('applications?page_size=100'), call('interviews?status=evaluated&page_size=8')]);
  A.system = system;
  const waiting = apps.items.filter((a) => ['submitted', 'needs_review'].includes(a.status)).slice(0, 8);
  const toReview = ivs.items.filter((i) => !i.decision);
  const card = (n, label, hash) => h('a', { class: 'card stat', href: hash || '#', style: null }, h('b', null, n), h('span', null, label));
  const health = [
    ['Admin sign-in', true, 'configured'],
    ['Gemini AI', system.ai.configured, system.ai.mock ? 'MOCK mode (no real AI)' : system.ai.configured ? `models: ${Object.values(system.ai.models).filter(Boolean).slice(0, 2).join(', ') || 'auto'}` : 'GEMINI_API_KEY missing'],
    ['Google SMTP', system.smtp.configured || system.smtp.dry_run, system.smtp.dry_run ? 'DRY RUN (emails not sent)' : system.smtp.configured ? `${system.smtp.user} via ${system.smtp.host}:${system.smtp.port}` : 'SMTP_USER / SMTP_PASSWORD missing'],
    ['Video storage', system.storage.free_mb < 0 || system.storage.free_mb > 500, `${system.storage.used_mb} MB used · ${system.storage.free_mb} MB free`],
    ['Data persistence', !(system.persistence?.warnings || []).length, (system.persistence?.warnings || []).length ? 'Temporary disk: data is erased on every deploy or restart' : `${system.database} database on a persistent volume`],
  ];
  const warnings = system.persistence?.warnings || [];
  mount(view,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Overview'), h('p', { class: 'muted' }, 'Recruitment pipeline at a glance.'))),
    warnings.length ? h('div', { class: 'notice danger' }, h('strong', null, 'Your recruitment data is not safe. '), warnings.map((w) => h('div', null, w))) : null,
    h('div', { class: 'grid four' },
      card(stats.positions_open, 'Open positions', '#/positions'), card(stats.awaiting_invite, 'Awaiting invite', '#/applications'),
      card(stats.invited, 'Invited / verifying', '#/applications'), card(stats.interviewing, 'Interviewing now', '#/interviews'),
      card(stats.completed, 'Interviews completed', '#/interviews'), card(stats.to_review, 'Ready for your review', '#/interviews'),
      card(stats.recordings, 'Recorded videos', '#/interviews'), card(`${stats.storage_mb} MB`, 'Video storage used', '#/system')),
    h('div', { class: 'section-title' }, h('h2', null, 'System health')),
    h('div', { class: 'card' }, h('div', { class: 'grid two' }, health.map(([n, ok, d]) => h('div', { class: 'row' }, h('span', { class: `badge ${ok ? 'ok' : 'danger'}` }, ok ? 'OK' : 'Action needed'), h('div', null, h('strong', null, n), h('div', { class: 'small muted' }, d)))))),
    h('div', { class: 'grid two' },
      h('div', null, h('div', { class: 'section-title' }, h('h2', null, 'Applications awaiting an invite'), link('#/applications', 'View all')),
        h('div', { class: 'card' }, waiting.length ? h('table', { class: 'tbl' }, h('tbody', null, waiting.map((a) => h('tr', { class: 'click', onclick: () => { location.hash = `#/applications/${a.id}`; } },
          h('td', null, h('strong', null, a.full_name), h('div', { class: 'tiny muted' }, a.position_title)), h('td', null, badge(APP_STATUS, a.status)),
          h('td', { class: 'tiny muted' }, ago(a.created_at)))))) : empty('No applications are waiting.'))),
      h('div', null, h('div', { class: 'section-title' }, h('h2', null, 'Interviews to review'), link('#/interviews', 'View all')),
        h('div', { class: 'card' }, toReview.length ? h('table', { class: 'tbl' }, h('tbody', null, toReview.map((i) => h('tr', { class: 'click', onclick: () => { location.hash = `#/interviews/${i.id}`; } },
          h('td', null, h('strong', null, i.full_name), h('div', { class: 'tiny muted' }, i.position_title)), h('td', null, i.overall_score !== null ? h('span', { class: `badge ${scoreKind(i.overall_score)}` }, `${i.overall_score}/100`) : '—'),
          h('td', null, badge(RECO, i.recommendation)))))) : empty('Nothing waiting for review.')))));
}

// ------------------------------------------------------------------ positions
async function viewPositions() {
  const items = await loadPositions();
  const base = location.origin;
  mount(view, h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Positions'), h('p', { class: 'muted' }, 'Roles applicants can apply for, with eligibility rules and AI interview questions.')),
    h('a', { class: 'btn', href: '#/positions/new' }, '+ New position')),
    h('div', { class: 'card table-wrap' }, items.length ? h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Position', 'Status', 'Applications', 'Interview', ''].map((x) => h('th', null, x)))),
      h('tbody', null, items.map((p) => h('tr', null,
        h('td', null, h('a', { href: `#/positions/${p.id}` }, h('strong', null, p.title)), h('div', { class: 'tiny muted' }, [p.department, p.location, p.employment_type].filter(Boolean).join(' · '))),
        h('td', null, h('span', { class: `badge ${p.status === 'open' ? 'ok' : ''}` }, p.status)),
        h('td', null, h('a', { href: `#/applications` }, p.application_count)),
        h('td', { class: 'small muted' }, `${p.config.questions.length} topics · ${p.config.interview.duration_minutes} min${p.config.auto_invite ? ' · auto-invite' : ''}`),
        h('td', null, h('div', { class: 'row-actions' },
          h('button', { class: 'btn secondary small', onclick: () => copy(`${base}/apply?position=${p.id}`, 'Application link copied') }, 'Copy apply link'),
          h('a', { class: 'btn secondary small', href: `#/positions/${p.id}` }, 'Edit'))))))) : empty('No positions yet. Create one to start receiving applications.')));
}

const blankPosition = () => ({ title: '', department: '', location: '', employment_type: 'Full-time', description: '', status: 'open', config: {
  required_skills: [], nice_to_have: [], seniority: '', eligibility: { min_experience_years: 0, min_education: '', must_have_skills: [], notes: '' }, objectives: '',
  questions: [{ text: '', intent: '', required: true, weight: 3, rubric: '' }], criteria: [{ label: '', description: '', weight: 3 }],
  interview: { duration_minutes: 15, language: 'English', speech_lang: 'en-IN', tone: 'friendly-professional', difficulty: 'standard', persona_name: 'Noah', allow_typing: true }, auto_invite: false } });

async function viewPositionEditor(id) {
  const p = id ? (await call(`positions/${id}`)).position : blankPosition();
  const c = p.config;
  const csv = (arr) => (arr || []).join(', ');
  const text = (obj, key, props = {}) => h('input', { type: 'text', value: obj[key] ?? '', oninput: (e) => { obj[key] = e.target.value; }, ...props });
  const area = (obj, key, rows = 3, props = {}) => h('textarea', { rows, oninput: (e) => { obj[key] = e.target.value; }, ...props }, obj[key] ?? '');
  const num = (obj, key, props = {}) => h('input', { type: 'number', value: obj[key] ?? '', oninput: (e) => { obj[key] = e.target.value === '' ? '' : Number(e.target.value); }, ...props });
  const sel = (obj, key, options) => h('select', { onchange: (e) => { obj[key] = e.target.value; } }, options.map(([v, l]) => h('option', { value: v, selected: String(obj[key]) === String(v) }, l)));
  const list = (obj, key) => h('input', { type: 'text', value: csv(obj[key]), placeholder: 'comma separated', oninput: (e) => { obj[key] = e.target.value.split(',').map((s) => s.trim()).filter(Boolean); } });
  const check = (obj, key, label) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: !!obj[key], onchange: (e) => { obj[key] = e.target.checked; } }), h('span', null, label));

  const qBox = h('div', { class: 'stack' }), cBox = h('div', { class: 'stack' });
  const drawQuestions = () => mount(qBox, c.questions.map((q, i) => h('div', { class: 'q-card' },
    h('div', { class: 'row between' }, h('strong', null, `Topic ${i + 1}`), h('button', { class: 'btn ghost small', type: 'button', onclick: () => { c.questions.splice(i, 1); drawQuestions(); } }, 'Remove')),
    field('Question / topic the AI should explore', area(q, 'text', 2, { placeholder: 'e.g. Tell me about debugging a hard production issue.' })),
    h('div', { class: 'grid two' }, field('What should this reveal?', text(q, 'intent', { placeholder: 'e.g. Troubleshooting depth' })), field('What does a strong answer look like?', text(q, 'rubric', { placeholder: 'e.g. Systematic, uses logs, explains root cause' }))),
    h('div', { class: 'row wrap' }, check(q, 'required', 'Must be covered'), h('span', { class: 'grow' }), h('label', { class: 'field' }, 'Weight'), h('div', { class: 'w-wt' }, sel(q, 'weight', [1, 2, 3, 4, 5].map((n) => [n, `${n}/5`])))))));
  const drawCriteria = () => mount(cBox, c.criteria.map((k, i) => h('div', { class: 'grid three' }, text(k, 'label', { placeholder: 'Criterion, e.g. Customer empathy' }), text(k, 'description', { placeholder: 'What a strong answer demonstrates' }),
    h('div', { class: 'row' }, sel(k, 'weight', [1, 2, 3, 4, 5].map((n) => [n, `weight ${n}`])), h('button', { class: 'btn ghost small', type: 'button', onclick: () => { c.criteria.splice(i, 1); drawCriteria(); } }, '✕')))));
  drawQuestions(); drawCriteria();

  async function suggest() {
    if (!p.title.trim()) return toast('Add a position title first.', 'error');
    const btn = $('#suggest-btn'); btn.disabled = true; btn.textContent = 'Thinking…';
    try {
      const out = (await call('positions/suggest', { method: 'POST', json: { title: p.title, department: p.department, description: p.description, required_skills: c.required_skills, seniority: c.seniority }, timeout: 90000 })).suggestions;
      const body = h('div', { class: 'stack' }, out.objectives && h('div', { class: 'notice' }, h('strong', null, 'Objective: '), out.objectives),
        out.questions.map((q) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: true, dataset: { kind: 'q' }, value: JSON.stringify(q) }), h('span', null, h('strong', null, q.text), h('div', { class: 'tiny muted' }, `${q.intent} · ${q.required ? 'required' : 'optional'} · weight ${q.weight}`)))),
        h('strong', null, 'Scoring criteria'), out.criteria.map((k) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: true, dataset: { kind: 'c' }, value: JSON.stringify(k) }), h('span', null, `${k.label} — ${k.description}`))));
      modal('AI suggestions', body, [{ label: 'Cancel', kind: 'secondary' }, { label: 'Add selected', onClick: () => {
        const picked = (kind) => $$(`input[data-kind="${kind}"]`, body).filter((i) => i.checked).map((i) => JSON.parse(i.value));
        c.questions = c.questions.filter((q) => q.text.trim()).concat(picked('q').map((q) => ({ text: q.text, intent: q.intent, required: q.required, weight: Math.min(5, Math.max(1, q.weight)), rubric: '' })));
        c.criteria = c.criteria.filter((k) => k.label.trim()).concat(picked('c').map((k) => ({ label: k.label, description: k.description, weight: 3 })));
        if (!c.objectives && out.objectives) { c.objectives = out.objectives; $('#objectives').value = out.objectives; }
        drawQuestions(); drawCriteria();
      } }]);
    } catch (e) { toast(e.message, 'error'); } finally { btn.disabled = false; btn.textContent = '✨ Suggest with AI'; }
  }

  async function save() {
    const btn = $('#save-btn'); btn.disabled = true;
    try {
      const payload = { ...p, ...c, eligibility: c.eligibility };
      const out = id ? await call(`positions/${id}`, { method: 'PUT', json: payload }) : await call('positions', { method: 'POST', json: payload });
      toast('Position saved', 'ok'); location.hash = '#/positions'; return out;
    } catch (e) { toast(e.message, 'error'); } finally { btn.disabled = false; }
  }

  mount(view,
    h('div', { class: 'page-head' }, h('div', null, h('h1', null, id ? 'Edit position' : 'New position'), h('p', { class: 'muted' }, 'Applicants see the basics and requirements; the interview topics and scoring stay private.')),
      h('div', { class: 'row' }, id && h('button', { class: 'btn secondary danger-text', onclick: guard(async () => { if (await confirmDialog('Delete position?', 'If it has applications it will be closed instead of deleted.', 'Delete')) { const r = await call(`positions/${id}`, { method: 'DELETE' }); toast(r.deleted ? 'Deleted' : 'Closed (has applications)', 'ok'); location.hash = '#/positions'; } }) }, 'Delete'), h('a', { class: 'btn secondary', href: '#/positions' }, 'Cancel'), h('button', { class: 'btn', id: 'save-btn', onclick: save }, 'Save position'))),
    h('div', { class: 'card stack' }, h('h3', null, 'Basics'),
      h('div', { class: 'grid two' }, field('Title *', text(p, 'title', { maxlength: 120 })), field('Department', text(p, 'department')), field('Location', text(p, 'location')),
        field('Employment type', sel(p, 'employment_type', ['Full-time', 'Part-time', 'Contract', 'Internship'].map((x) => [x, x]))), field('Seniority', text(c, 'seniority', { placeholder: 'e.g. Senior' })),
        field('Status', sel(p, 'status', [['open', 'Open for applications'], ['closed', 'Closed']]))),
      field('Description', area(p, 'description', 5, { maxlength: 6000 })),
      h('div', { class: 'grid two' }, field('Required skills', list(c, 'required_skills')), field('Nice-to-have skills', list(c, 'nice_to_have')))),
    h('div', { class: 'card stack' }, h('h3', null, 'Eligibility requirements'), h('p', { class: 'small muted' }, 'Applications are screened against these automatically. Candidates who miss them are flagged for your review — never auto-rejected.'),
      h('div', { class: 'grid three' }, field('Minimum experience (years)', num(c.eligibility, 'min_experience_years', { min: 0, max: 50, step: 0.5 })), field('Minimum education', sel(c.eligibility, 'min_education', Object.entries(EDU))), field('Must-have skills', list(c.eligibility, 'must_have_skills'))),
      field('Other notes shown to applicants', area(c.eligibility, 'notes', 2)), check(c, 'auto_invite', 'Auto-invite: automatically generate a code and email applicants who meet every requirement')),
    h('div', { class: 'card stack' }, h('div', { class: 'row between' }, h('h3', null, 'Interview topics'), h('button', { class: 'btn secondary small', id: 'suggest-btn', type: 'button', onclick: suggest }, '✨ Suggest with AI')),
      h('p', { class: 'small muted' }, 'These are topic seeds, not a script. The AI interviewer rewrites them in its own words, asks many follow-ups tailored to each applicant’s answers and background, and makes sure required topics are covered.'),
      field('Interview objectives', area(c, 'objectives', 2, { id: 'objectives', placeholder: 'What should this interview establish?' })), qBox,
      h('button', { class: 'btn secondary small', type: 'button', onclick: () => { c.questions.push({ text: '', intent: '', required: true, weight: 3, rubric: '' }); drawQuestions(); } }, '+ Add topic')),
    h('div', { class: 'card stack' }, h('h3', null, 'Scoring criteria'), h('p', { class: 'small muted' }, 'In addition to knowledge, communication, reasoning, confidence and role fit, the AI scores each of these 0–10.'), cBox,
      h('button', { class: 'btn secondary small', type: 'button', onclick: () => { c.criteria.push({ label: '', description: '', weight: 3 }); drawCriteria(); } }, '+ Add criterion')),
    h('div', { class: 'card stack' }, h('h3', null, 'Interview settings'),
      h('div', { class: 'grid three' }, field('Target length (minutes)', num(c.interview, 'duration_minutes', { min: 5, max: 45 })), field('Interviewer name', text(c.interview, 'persona_name')),
        field('Tone', sel(c.interview, 'tone', [['friendly-professional', 'Friendly & professional'], ['formal', 'Formal'], ['casual', 'Casual']])),
        field('Difficulty', sel(c.interview, 'difficulty', [['easy', 'Easy'], ['standard', 'Standard'], ['challenging', 'Challenging']])), field('Interview language', text(c.interview, 'language')),
        field('Speech recognition locale', text(c.interview, 'speech_lang', { placeholder: 'en-IN' }), 'BCP-47 code, e.g. en-IN, en-US, en-GB')),
      check(c.interview, 'allow_typing', 'Let applicants type an answer when speaking is difficult')));
}

// ------------------------------------------------------------------ applications
const AF = { position: '', status: '', q: '', page: 1 };
async function viewApplications() {
  await loadPositions();
  const selected = new Set();
  const holder = h('div');
  async function draw() {
    const qs = new URLSearchParams({ page: AF.page, page_size: 25 });
    ['position', 'status', 'q'].forEach((k) => AF[k] && qs.set(k, AF[k]));
    const data = await call(`applications?${qs}`);
    selected.clear();
    const bulkBtn = h('button', { class: 'btn small', disabled: true, onclick: bulkInvite }, 'Invite selected');
    const updateBulk = () => { bulkBtn.disabled = !selected.size; bulkBtn.textContent = selected.size ? `Invite ${selected.size} selected` : 'Invite selected'; };
    mount(holder, h('div', { class: 'row between', style: null }, h('span'), bulkBtn),
      h('div', { class: 'card table-wrap' }, data.items.length ? h('table', { class: 'tbl' },
        h('thead', null, h('tr', null, ['', 'Applicant', 'Position', 'Eligibility', 'Status', 'Interview code', 'Applied', ''].map((x) => h('th', null, x)))),
        h('tbody', null, data.items.map((a) => {
          const en = a.enrollment;
          const canInvite = !(en && en.status === 'completed');
          return h('tr', null,
            h('td', null, canInvite && h('input', { type: 'checkbox', 'aria-label': `Select ${a.full_name}`, onchange: (e) => { e.target.checked ? selected.add(a.id) : selected.delete(a.id); updateBulk(); } })),
            h('td', { class: 'click-cell' }, link(`#/applications/${a.id}`, h('strong', null, a.full_name)), h('div', { class: 'tiny muted' }, a.email)),
            h('td', null, a.position_title), h('td', null, h('span', { class: `badge ${a.eligibility.passed ? 'ok' : 'warn'}`, title: a.eligibility.summary }, a.eligibility.passed ? 'Meets requirements' : 'Review')),
            h('td', null, a.decision ? badge(DECISION, a.decision) : badge(APP_STATUS, a.status)),
            h('td', null, en ? h('div', null, h('span', { class: 'code-pill' }, en.code), h('div', { class: 'tiny' }, badge(ENR_STATUS, en.status))) : h('span', { class: 'muted' }, '—')),
            h('td', { class: 'tiny muted' }, fmtDate(a.created_at, false)),
            h('td', null, h('div', { class: 'row-actions' }, canInvite && h('button', { class: 'btn small', onclick: () => inviteDialog(a) }, en ? 'New code' : 'Generate code & email'),
              en && ['issued', 'opened', 'verified'].includes(en.status) && h('button', { class: 'btn secondary small', onclick: guard(async () => { await call(`applications/${a.id}/resend`, { method: 'POST', json: {} }); toast('Invitation re-sent', 'ok'); draw(); }) }, 'Resend'))));
        }))) : empty('No applications match.')), pagerEl(data, (pg) => { AF.page = pg; draw(); }));
  }
  const bulkInvite = guard(async () => {
    if (!selected.size || !(await confirmDialog('Send invitations?', `Generate enrollment codes and email ${selected.size} applicant(s).`, 'Send', ''))) return;
    const out = await call('applications/bulk-invite', { method: 'POST', json: { ids: [...selected] } });
    const ok = out.results.filter((r) => r.ok).length;
    toast(`${ok} invited${ok < out.results.length ? `, ${out.results.length - ok} failed` : ''}`, ok ? 'ok' : 'error'); draw();
  });

  function inviteDialog(a) {
    const days = h('input', { type: 'number', min: 1, max: 30, value: 7 }), msg = h('textarea', { rows: 3, maxlength: 600, placeholder: 'Optional personal note included in the email' });
    modal(`Invite ${a.full_name}`, h('div', { class: 'stack' }, h('p', { class: 'muted small' }, `A unique enrollment code will be generated and emailed to ${a.email} through Google SMTP.`), field('Code valid for (days)', days), field('Personal message', msg)),
      [{ label: 'Cancel', kind: 'secondary' }, { label: 'Generate code & send email', onClick: async () => {
        try { const out = await call(`applications/${a.id}/invite`, { method: 'POST', json: { expires_days: Number(days.value), message: msg.value } }); toast(`Code ${out.enrollment.code} generated — email queued`, 'ok'); draw(); }
        catch (e) { toast(e.message, 'error'); return false; }
      } }]);
  }
  const addBtn = h('button', { class: 'btn', onclick: addApplicantDialog }, '+ Add applicant');
  function addApplicantDialog() {
    const f = { position_id: A.positions.find((p) => p.status === 'open')?.id || '', full_name: '', email: '', phone: '', experience_years: '', skills: '' };
    const inp = (k, props = {}) => h('input', { type: 'text', oninput: (e) => { f[k] = e.target.value; }, ...props });
    const invite = h('input', { type: 'checkbox', checked: true });
    modal('Add applicant manually', h('div', { class: 'stack' }, field('Position', h('select', { onchange: (e) => { f.position_id = e.target.value; } }, A.positions.map((p) => h('option', { value: p.id, selected: p.id === f.position_id }, p.title)))),
      field('Full name *', inp('full_name')), field('Email *', inp('email', { type: 'email' })), h('div', { class: 'grid two' }, field('Phone', inp('phone')), field('Years of experience', inp('experience_years', { type: 'number' }))), field('Skills', inp('skills')),
      h('label', { class: 'check' }, invite, h('span', null, 'Generate a code and send the invitation email right away'))),
      [{ label: 'Cancel', kind: 'secondary' }, { label: 'Add', onClick: async () => { try { await call('applications', { method: 'POST', json: { ...f, invite: invite.checked } }); toast('Applicant added', 'ok'); draw(); } catch (e) { toast(e.message, 'error'); return false; } } }]);
  }
  mount(view, h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Applications'), h('p', { class: 'muted' }, 'Review applicants, generate enrollment codes and send interview invitations.')), addBtn),
    h('div', { class: 'filters' }, h('input', { type: 'search', placeholder: 'Search name or email', value: AF.q, oninput: debounce((e) => { AF.q = e.target.value; AF.page = 1; draw(); }) }),
      positionSelect(AF.position, (v) => { AF.position = v; AF.page = 1; draw(); }),
      h('select', { onchange: (e) => { AF.status = e.target.value; AF.page = 1; draw(); } }, [['', 'All statuses'], ['submitted', 'New'], ['needs_review', 'Needs review'], ['invited', 'Invited'], ['face_verified', 'Face verified'], ['interviewing', 'Interviewing'], ['interviewed', 'Interviewed'], ['evaluated', 'Evaluated'], ['decision:shortlisted', 'Shortlisted'], ['decision:rejected', 'Rejected']].map(([v, l]) => h('option', { value: v, selected: AF.status === v }, l)))),
    holder);
  await draw();
}

function debounce(fn, ms = 300) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function viewApplication(id) {
  const { application: a } = await call(`applications/${id}`);
  const p = a.profile || {};
  const en = a.enrollments[0];
  const kv = (rows) => h('dl', { class: 'kv' }, rows.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => [h('dt', null, k), h('dd', null, v)]));
  const decisionBar = h('div', { class: 'decision-bar' }, ['shortlisted', 'on_hold', 'rejected', 'hired'].map((d) => h('button', { class: `btn secondary small ${a.decision === d ? 'on' : ''}`, onclick: guard(() => setDecision(id, a.decision, d, () => viewApplication(id))) }, DECISION[d][0])));
  const notes = h('textarea', { rows: 4, maxlength: 4000, placeholder: 'Internal notes (never shown to the applicant)' }, a.admin_notes || '');
  const iv = a.interviews[0];
  mount(view,
    h('div', { class: 'page-head' }, h('div', null, link('#/applications', '← Applications'), h('h1', null, a.full_name), h('p', { class: 'muted' }, `${a.email}${a.phone ? ' · ' + a.phone : ''} · applied ${fmtDate(a.created_at)}`)),
      h('div', { class: 'row' }, badge(APP_STATUS, a.status), a.decision && badge(DECISION, a.decision))),
    h('div', { class: 'grid two' },
      h('div', { class: 'stack' },
        h('div', { class: 'card' }, h('h3', null, 'Applying for'), h('p', null, h('strong', null, a.position?.title || '—')),
          h('div', { class: `notice ${a.eligibility.passed ? 'ok' : 'warn'}` }, h('strong', null, a.eligibility.passed ? 'Meets stated requirements' : 'Needs review'), a.eligibility.checks.length ? h('ul', { class: 'plain' }, a.eligibility.checks.map((c) => h('li', null, `${c.passed ? '✓' : '✗'} ${c.name}: ${c.detail}`))) : h('div', { class: 'small' }, 'No eligibility rules set for this position.'))),
        h('div', { class: 'card' }, h('h3', null, 'Profile'), kv([['Experience', p.experience_years !== null && p.experience_years !== undefined ? `${p.experience_years} years` : ''], ['Education', EDU[p.education] || ''], ['Current role', [p.current_title, p.current_company].filter(Boolean).join(' at ')], ['Location', p.location], ['Notice period', p.notice_period],
          ['Skills', p.skills?.length ? h('div', { class: 'chips' }, p.skills.map((s) => h('span', { class: 'chip' }, s))) : ''], ['LinkedIn', p.linkedin && h('a', { href: p.linkedin, target: '_blank', rel: 'noopener noreferrer' }, p.linkedin)], ['Portfolio', p.portfolio && h('a', { href: p.portfolio, target: '_blank', rel: 'noopener noreferrer' }, p.portfolio)]]),
          p.cover_note && h('div', null, h('h4', { class: 'muted' }, 'Cover note'), h('p', { class: 'small pre' }, p.cover_note)),
          a.has_resume && h('a', { class: 'btn secondary small', href: `/staff/api/applications/${id}/resume` }, '⬇ Download resume'), a.resume_excerpt && h('details', null, h('summary', { class: 'small muted' }, 'Resume text (extracted)'), h('p', { class: 'small pre' }, a.resume_excerpt)))),
      h('div', { class: 'stack' },
        h('div', { class: 'card' }, h('h3', null, 'Interview enrollment'),
          en ? h('div', { class: 'stack' }, h('div', { class: 'row wrap' }, h('span', { class: 'code-pill' }, en.code), badge(ENR_STATUS, en.status), h('button', { class: 'btn ghost small', onclick: () => copy(en.code, 'Code copied') }, 'Copy code'), h('button', { class: 'btn ghost small', onclick: () => copy(`${location.origin}/join?code=${en.code}`, 'Interview link copied') }, 'Copy link')),
            h('div', { class: 'small muted' }, `Expires ${fmtDate(en.expires_at)} · face-check attempts ${en.face_attempts}`),
            h('div', { class: 'row wrap' }, h('button', { class: 'btn small', onclick: guard(async () => { await call(`applications/${id}/invite`, { method: 'POST', json: {} }); toast('New code generated and emailed', 'ok'); viewApplication(id); }) }, 'Generate new code & email'),
              ['issued', 'opened', 'verified'].includes(en.status) && h('button', { class: 'btn secondary small', onclick: guard(async () => { await call(`applications/${id}/resend`, { method: 'POST', json: {} }); toast('Invitation re-sent', 'ok'); viewApplication(id); }) }, 'Resend email'),
              ['issued', 'opened', 'verified', 'in_progress'].includes(en.status) && h('button', { class: 'btn secondary small', onclick: guard(async () => { if (await confirmDialog('Revoke this code?', 'The applicant will no longer be able to use it.', 'Revoke')) { await call(`enrollments/${en.id}/revoke`, { method: 'POST' }); viewApplication(id); } }) }, 'Revoke'),
              en.face_attempts > 0 && en.status !== 'completed' && h('button', { class: 'btn secondary small', onclick: guard(async () => { await call(`enrollments/${en.id}/reset-face`, { method: 'POST' }); toast('Face check reset', 'ok'); viewApplication(id); }) }, 'Reset face check')))
            : h('div', { class: 'stack' }, h('p', { class: 'muted small' }, 'No enrollment code yet. Generating one emails the applicant their personal interview link.'),
              h('button', { class: 'btn', onclick: guard(async () => { await call(`applications/${id}/invite`, { method: 'POST', json: {} }); toast('Code generated — email queued', 'ok'); viewApplication(id); }) }, 'Generate code & email'))),
        iv && h('div', { class: 'card' }, h('h3', null, 'Interview'), h('div', { class: 'row wrap' }, badge(IV_STATUS, iv.status), iv.overall_score !== null && h('span', { class: `badge ${scoreKind(iv.overall_score)}` }, `${iv.overall_score}/100`), badge(RECO, iv.recommendation), h('a', { class: 'btn small', href: `#/interviews/${iv.id}` }, 'Open interview & video'))),
        h('div', { class: 'card' }, h('h3', null, 'Decision'), decisionBar, h('div', { class: 'section-title' }, h('h4', null, 'Notes')), notes, h('div', { style: null }, h('button', { class: 'btn small', onclick: guard(async () => { await call(`applications/${id}`, { method: 'PATCH', json: { admin_notes: notes.value } }); toast('Notes saved', 'ok'); }) }, 'Save notes'))),
        h('div', { class: 'card' }, h('h3', null, 'Emails sent'), a.emails.length ? h('table', { class: 'tbl' }, h('tbody', null, a.emails.map((m) => h('tr', null, h('td', null, h('strong', null, m.kind), h('div', { class: 'tiny muted' }, fmtDate(m.created_at))), h('td', null, h('span', { class: `badge ${m.status === 'sent' ? 'ok' : m.status === 'failed' ? 'danger' : 'info'}` }, m.status), m.error && h('div', { class: 'tiny muted' }, m.error)))))) : empty('No emails yet.')),
        h('div', { class: 'card' }, h('h3', null, 'Danger zone'), h('p', { class: 'small muted' }, 'Permanently deletes this applicant, their resume, face photo, transcript and recorded video.'), h('button', { class: 'btn danger small', onclick: guard(async () => { if (await confirmDialog('Delete applicant and all data?', `This permanently removes ${a.full_name}'s application, recordings and assessments. It cannot be undone.`, 'Delete everything')) { await call(`applications/${id}`, { method: 'DELETE' }); toast('Deleted', 'ok'); location.hash = '#/applications'; } }) }, 'Delete all data')))));
  // email delivery status updates after send
  if (a.emails.some((m) => m.status === 'queued')) A.poll = setInterval(() => { if (location.hash === `#/applications/${id}`) viewApplication(id).catch(() => {}); }, 4000);
}

// ------------------------------------------------------------------ interviews & videos
const IF = { position: '', status: '', q: '', page: 1 };
async function viewInterviews() {
  await loadPositions();
  const holder = h('div');
  async function draw() {
    const qs = new URLSearchParams({ page: IF.page, page_size: 25 });
    ['position', 'status', 'q'].forEach((k) => IF[k] && qs.set(k, IF[k]));
    const data = await call(`interviews?${qs}`);
    mount(holder, h('div', { class: 'card table-wrap' }, data.items.length ? h('table', { class: 'tbl' },
      h('thead', null, h('tr', null, ['Applicant', 'Position', 'Code', 'Status', 'AI score', 'Recommendation', 'Video', 'Decision', 'Date'].map((x) => h('th', null, x)))),
      h('tbody', null, data.items.map((i) => h('tr', { class: 'click', onclick: () => { location.hash = `#/interviews/${i.id}`; } },
        h('td', null, h('strong', null, i.full_name), h('div', { class: 'tiny muted' }, i.email)), h('td', null, i.position_title), h('td', null, h('span', { class: 'code-pill' }, i.code)),
        h('td', null, badge(IV_STATUS, i.status)), h('td', null, i.overall_score !== null ? h('span', { class: `badge ${scoreKind(i.overall_score)}` }, `${i.overall_score}/100`) : '—'), h('td', null, i.recommendation ? badge(RECO, i.recommendation) : '—'),
        h('td', { class: 'small' }, i.recordings ? `▶ ${fmtDuration(i.recording_seconds)} · ${i.recording_mb} MB` : h('span', { class: 'muted' }, 'none')), h('td', null, i.decision ? badge(DECISION, i.decision) : '—'), h('td', { class: 'tiny muted' }, fmtDate(i.started_at)))))) : empty('No interviews yet.')),
      pagerEl(data, (pg) => { IF.page = pg; draw(); }));
  }
  mount(view, h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Interviews & videos'), h('p', { class: 'muted' }, 'Every recorded interview with its transcript and AI assessment.'))),
    h('div', { class: 'filters' }, h('input', { type: 'search', placeholder: 'Search name, email or code', value: IF.q, oninput: debounce((e) => { IF.q = e.target.value; IF.page = 1; draw(); }) }), positionSelect(IF.position, (v) => { IF.position = v; IF.page = 1; draw(); }),
      h('select', { onchange: (e) => { IF.status = e.target.value; IF.page = 1; draw(); } }, [['', 'All statuses'], ['in_progress', 'In progress'], ['evaluating', 'Evaluating'], ['evaluated', 'Evaluated'], ['evaluation_failed', 'Evaluation failed']].map(([v, l]) => h('option', { value: v, selected: IF.status === v }, l)))), holder);
  await draw();
}

async function viewInterview(id) {
  const { interview: iv } = await call(`interviews/${id}`);
  const a = iv.application, ev = iv.evaluation, p = a.profile || {};
  const recs = iv.recordings;
  let tab = 'assessment';
  const body = h('div');
  const player = h('div', { class: 'player' });
  let video = null, segIdx = 0;

  function drawPlayer() {
    if (!recs.length) { mount(player, h('div', { class: 'card empty' }, iv.status === 'in_progress' ? 'The interview is still in progress.' : 'No video was uploaded for this interview.')); return; }
    const rec = recs[segIdx];
    video = h('video', { controls: true, preload: 'metadata', playsinline: true, src: `/staff/api/recordings/${rec.id}/stream` });
    // Browser-made WebM has no duration header (video.duration === Infinity): seek to the end once so the
    // browser computes it, which also makes the scrub bar usable.
    video.addEventListener('loadedmetadata', () => {
      if (video.duration !== Infinity) return;
      const fix = () => { video.removeEventListener('timeupdate', fix); video.currentTime = 0; };
      video.addEventListener('timeupdate', fix);
      video.currentTime = 1e101;
    });
    const speed = h('select', { onchange: (e) => { video.playbackRate = Number(e.target.value); } }, [0.75, 1, 1.25, 1.5, 2].map((s) => h('option', { value: s, selected: s === 1 }, `${s}×`)));
    mount(player, video, h('div', { class: 'row wrap', style: null }, recs.length > 1 && h('select', { onchange: (e) => { segIdx = Number(e.target.value); drawPlayer(); } }, recs.map((r, i) => h('option', { value: i, selected: i === segIdx }, `Part ${i + 1}${r.status !== 'complete' ? ' (incomplete)' : ''}`))), speed,
      h('span', { class: 'tiny muted' }, `${(rec.size / 1048576).toFixed(1)} MB · ${rec.duration_ms ? fmtDuration(rec.duration_ms / 1000) : 'duration pending'} · ${rec.status}`), h('span', { class: 'grow' }),
      h('a', { class: 'btn secondary small', href: `/staff/api/recordings/${rec.id}/download` }, '⬇ Download'),
      h('button', { class: 'btn ghost small', onclick: guard(async () => { if (await confirmDialog('Delete this video?', 'The recording file is permanently removed. The transcript and assessment stay.', 'Delete video')) { await call(`recordings/${rec.id}`, { method: 'DELETE' }); toast('Video deleted', 'ok'); viewInterview(id); } }) }, 'Delete video')));
  }

  function seekToTurn(t) {
    if (!video || !recs.length) return;
    const rec = recs[segIdx]; const abs = iv.started_at + t; const recStart = rec.created_at - 4500;       // first 4 s chunk lands ~4.5 s after start
    video.currentTime = Math.max(0, (abs - recStart) / 1000 - 1);
    video.play().catch(() => {});
  }

  function assessment() {
    if (iv.status === 'in_progress') return h('div', { class: 'card empty' }, 'The interview is in progress.');
    if (iv.status === 'evaluating') { A.poll = setInterval(() => { if (location.hash === `#/interviews/${id}`) viewInterview(id).catch(() => {}); }, 5000); return h('div', { class: 'card empty' }, h('span', { class: 'spinner' }), h('p', null, 'The AI is evaluating this interview…')); }
    if (!ev) return h('div', { class: 'card stack' }, h('div', { class: 'notice danger' }, `Evaluation failed${iv.state_error ? ': ' + iv.state_error : ''}.`), h('button', { class: 'btn', onclick: guard(async () => { await call(`interviews/${id}/reevaluate`, { method: 'POST' }); viewInterview(id); }) }, 'Re-run AI evaluation'));
    const score = ev.overall_score;
    const ring = h('div', { class: 'score-ring' }, h('b', null, score === null ? '—' : score));
    if (score !== null) { ring.style.setProperty('--p', score); ring.style.setProperty('--c', `var(--${scoreKind(score)})`); }
    return h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('div', { class: 'row wrap' }, ring, h('div', { class: 'grow' }, h('div', { class: 'row wrap' }, badge(RECO, ev.recommendation), h('span', { class: 'badge' }, `Assessment confidence: ${ev.confidence_in_assessment}`)), h('p', { style: null, class: 'small muted' }, ev.confidence_note))),
        h('p', null, ev.summary), h('div', { class: 'notice' }, h('strong', null, 'Suitability: '), ev.suitability)),
      ev.dimensions.length > 0 && h('div', { class: 'card' }, h('h3', null, 'Scores'), ev.dimensions.map((d) => [h('div', { class: 'dim' }, h('span', null, d.name.replace('_', ' ')), h('div', { class: 'bar' }, (() => { const i = h('i', { class: scoreKind(d.score * 10) }); i.style.width = `${d.score * 10}%`; return i; })()), h('b', null, d.score)), h('div', { class: 'dim-note' }, d.rationale)])),
      h('div', { class: 'grid two' }, h('div', { class: 'card' }, h('h3', null, 'Strengths'), ev.strengths.length ? h('ul', { class: 'plain' }, ev.strengths.map((s) => h('li', null, s))) : h('p', { class: 'muted small' }, '—')),
        h('div', { class: 'card' }, h('h3', null, 'Concerns'), ev.concerns.length ? h('ul', { class: 'plain' }, ev.concerns.map((s) => h('li', null, s))) : h('p', { class: 'muted small' }, '—'))),
      ev.red_flags.length > 0 && h('div', { class: 'notice danger' }, h('strong', null, 'Red flags'), h('ul', { class: 'plain' }, ev.red_flags.map((s) => h('li', null, s)))),
      ev.topic_assessments.length > 0 && h('div', { class: 'card' }, h('h3', null, 'Answers by topic'), ev.topic_assessments.map((t) => h('div', { class: 'ta' }, h('div', { class: 'row between' }, h('strong', null, t.question_asked), t.score >= 0 ? h('span', { class: `badge ${scoreKind(t.score * 10)}` }, `${t.score}/10`) : h('span', { class: 'badge' }, 'not covered')),
        h('p', { class: 'small' }, t.answer_summary), t.evidence.map((q) => h('blockquote', { class: 'ev' }, `“${q}”`)), t.gaps && h('div', { class: 'tiny muted' }, `Gaps: ${t.gaps}`)))),
      ev.suggested_followups.length > 0 && h('div', { class: 'card' }, h('h3', null, 'Suggested follow-ups for a human interviewer'), h('ul', { class: 'plain' }, ev.suggested_followups.map((s) => h('li', null, s)))),
      ev.integrity_notes && h('div', { class: 'notice' }, h('strong', null, 'Session signals: '), ev.integrity_notes),
      h('button', { class: 'btn secondary small', onclick: guard(async () => { await call(`interviews/${id}/reevaluate`, { method: 'POST' }); toast('Re-evaluating…', 'ok'); viewInterview(id); }) }, 'Re-run AI evaluation'));
  }

  function transcript() {
    return h('div', { class: 'card' }, h('p', { class: 'tiny muted' }, 'Click a line to jump to that moment in the video (approximate).'),
      h('div', { class: 'turns' }, iv.turns.map((t) => h('div', { class: `turn ${t.role === 'candidate' ? 'cand' : ''}`, onclick: () => seekToTurn(t.t) }, h('div', { class: 'who' }, h('span', null, t.role === 'ai' ? 'Interviewer' : a.full_name), h('span', null, fmtDuration(t.t / 1000))), t.text))));
  }

  function applicantTab() {
    const face = iv.enrollment?.face || {};
    return h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('h3', null, 'Applicant'), h('dl', { class: 'kv' }, [['Name', a.full_name], ['Email', a.email], ['Phone', a.phone], ['Applying for', iv.position.title], ['Experience', p.experience_years !== null && p.experience_years !== undefined ? `${p.experience_years} years` : ''], ['Education', EDU[p.education]], ['Current role', [p.current_title, p.current_company].filter(Boolean).join(' at ')], ['Skills', (p.skills || []).join(', ')], ['Code', iv.enrollment?.code]].filter(([, v]) => v).map(([k, v]) => [h('dt', null, k), h('dd', null, v)])),
        link(`#/applications/${a.id}`, 'Open full application →')),
      h('div', { class: 'card' }, h('h3', null, 'Face verification'), iv.enrollment?.has_photo ? h('img', { class: 'photo', alt: 'Verification photo', src: `/staff/api/interviews/${id}/face.jpg` }) : h('p', { class: 'muted small' }, 'No photo on file.'),
        h('dl', { class: 'kv' }, [['Verified', iv.enrollment?.face_verified_at ? fmtDate(iv.enrollment.face_verified_at) : 'No'], ['Liveness steps', (face.tasks || []).join(', ')], ['AI photo check', face.server_check?.status], ['Detector', face.detector]].filter(([, v]) => v).map(([k, v]) => [h('dt', null, k), h('dd', null, v)]))),
      iv.plan_summary && h('div', { class: 'card' }, h('h3', null, 'Interviewer’s read of the background'), h('p', { class: 'small' }, iv.plan_summary)));
  }

  function coverageTab() {
    return h('div', { class: 'stack' },
      h('div', { class: 'card' }, h('h3', null, 'Topic coverage'), h('table', { class: 'tbl' }, h('tbody', null, iv.coverage.map((t) => h('tr', null, h('td', null, h('strong', null, t.id), h('div', { class: 'small muted' }, t.text)), h('td', null, h('span', { class: `badge ${t.status === 'covered' ? 'ok' : t.status === 'in_progress' ? 'warn' : ''}` }, t.status), t.required && h('span', { class: 'badge info' }, 'required')), h('td', { class: 'small' }, `depth ${t.depth}/3 · asked ${t.asked}×`)))))),
      h('div', { class: 'card' }, h('h3', null, 'Session signals'), iv.events.length ? h('table', { class: 'tbl' }, h('tbody', null, iv.events.map((e) => h('tr', null, h('td', null, e.type.replace('_', ' ')), h('td', { class: 'small muted' }, `at ${fmtDuration(e.t / 1000)}`), h('td', { class: 'small' }, e.duration_ms ? `${Math.round(e.duration_ms / 1000)} s` : '—'))))) : h('p', { class: 'muted small' }, 'No presence or tab-focus signals were recorded.'),
        h('p', { class: 'tiny muted' }, 'Signals are informational (e.g. a face briefly out of frame) and are not proof of wrongdoing. They do not change the AI score.'), iv.flags.length > 0 && h('div', { class: 'notice warn' }, h('strong', null, 'Interviewer flags: '), iv.flags.join(' · '))));
  }

  const decisionBar = h('div', { class: 'decision-bar' }, ['shortlisted', 'on_hold', 'rejected', 'hired'].map((d) => h('button', { class: `btn secondary small ${a.decision === d ? 'on' : ''}`, onclick: guard(() => setDecision(a.id, a.decision, d, () => viewInterview(id))) }, DECISION[d][0])));

  function drawTab() {
    mount(body, tab === 'assessment' ? assessment() : tab === 'transcript' ? transcript() : tab === 'applicant' ? applicantTab() : coverageTab());
    $$('.tabs button', view).forEach((b) => b.classList.toggle('active', b.dataset.tab === tab));
  }
  mount(view,
    h('div', { class: 'page-head' }, h('div', null, link('#/interviews', '← Interviews & videos'), h('h1', null, a.full_name), h('p', { class: 'muted' }, `${iv.position.title} · ${fmtDate(iv.started_at)} · code ${iv.enrollment?.code || ''}`)),
      h('div', { class: 'stack', style: null }, h('div', { class: 'row wrap' }, badge(IV_STATUS, iv.status), a.decision && badge(DECISION, a.decision)), decisionBar)),
    h('div', { class: 'iv-layout' }, h('div', { class: 'stack' }, player, h('div', { class: 'card' }, h('h4', null, 'Interview checks'), h('div', { class: 'row wrap' }, h('span', { class: `badge ${iv.enrollment?.face_verified_at ? 'ok' : 'danger'}` }, iv.enrollment?.face_verified_at ? 'Face verified' : 'Face not verified'),
      h('span', { class: `badge ${recs.length ? 'ok' : 'warn'}` }, recs.length ? 'Video stored' : 'No video'), h('span', { class: `badge ${iv.events.length ? 'warn' : 'ok'}` }, iv.events.length ? `${iv.events.length} session signal(s)` : 'No session signals')))),
      h('div', null, h('div', { class: 'tabs' }, [['assessment', 'AI assessment'], ['transcript', 'Transcript'], ['applicant', 'Applicant'], ['coverage', 'Coverage & signals']].map(([k, l]) => h('button', { dataset: { tab: k }, onclick: () => { tab = k; drawTab(); } }, l))), body)));
  drawPlayer(); drawTab();
}

// ------------------------------------------------------------------ emails
async function viewEmails() {
  const { items } = await call('emails?limit=200');
  mount(view, h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'Email log'), h('p', { class: 'muted' }, 'Every email sent through Google SMTP, with delivery status.'))),
    h('div', { class: 'card table-wrap' }, items.length ? h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['When', 'To', 'Type', 'Subject', 'Status', 'Tries'].map((x) => h('th', null, x)))),
      h('tbody', null, items.map((m) => h('tr', null, h('td', { class: 'tiny muted' }, fmtDate(m.created_at)), h('td', null, m.to_addr), h('td', null, m.kind), h('td', { class: 'small' }, m.subject),
        h('td', null, h('span', { class: `badge ${m.status === 'sent' ? 'ok' : m.status === 'failed' ? 'danger' : 'info'}` }, m.status), m.error && h('div', { class: 'tiny muted' }, m.error)), h('td', null, m.attempts)))))
      : empty('No emails yet.')));
}

// ------------------------------------------------------------------ system
async function viewSystem() {
  const [{ system: s }, audit] = await Promise.all([call('system'), call('audit?limit=60')]);
  const to = h('input', { type: 'email', placeholder: 'you@example.com' });
  const aiOut = h('div', { class: 'small muted' });
  mount(view, h('div', { class: 'page-head' }, h('div', null, h('h1', null, 'System'), h('p', { class: 'muted' }, 'Configuration and diagnostics. Secrets are read from environment variables and are never shown here.'))),
    h('div', { class: 'grid two' },
      h('div', { class: 'card stack' }, h('h3', null, 'Gemini AI'), h('div', { class: 'row' }, h('span', { class: `badge ${s.ai.configured ? 'ok' : 'danger'}` }, s.ai.mock ? 'Mock' : s.ai.configured ? 'Configured' : 'Missing GEMINI_API_KEY')),
        s.ai.configured && h('dl', { class: 'kv' }, Object.entries(s.ai.models).map(([k, v]) => [h('dt', null, k), h('dd', { class: 'mono small' }, v || 'auto')])), s.ai.last_error && h('div', { class: 'notice warn' }, s.ai.last_error),
        h('div', null, h('button', { class: 'btn secondary small', onclick: guard(async () => { aiOut.textContent = 'Testing…'; try { const r = await call('system/test-ai', { method: 'POST', timeout: 90000 }); aiOut.textContent = `OK — ${r.model} replied in ${r.latency_ms} ms`; } catch (e) { aiOut.textContent = `Failed: ${e.message}`; } }) }, 'Test Gemini'), aiOut)),
      h('div', { class: 'card stack' }, h('h3', null, 'Google SMTP'), h('div', { class: 'row' }, h('span', { class: `badge ${s.smtp.configured ? 'ok' : s.smtp.dry_run ? 'warn' : 'danger'}` }, s.smtp.dry_run ? 'Dry run (not sending)' : s.smtp.configured ? 'Configured' : 'Not configured')),
        h('dl', { class: 'kv' }, [['Host', `${s.smtp.host}:${s.smtp.port}`], ['Account', s.smtp.user || '—'], ['From name', s.smtp.from_name]].map(([k, v]) => [h('dt', null, k), h('dd', null, v)])),
        !s.smtp.configured && !s.smtp.dry_run && h('div', { class: 'notice warn' }, 'Set SMTP_USER (full Gmail address) and SMTP_PASSWORD (a 16-character Google App Password) in the server environment.'),
        h('div', { class: 'row' }, to, h('button', { class: 'btn secondary small', onclick: guard(async () => { try { await call('system/test-email', { method: 'POST', json: { to: to.value } }); toast('Test email sent', 'ok'); } catch (e) { toast(e.message, 'error'); } }) }, 'Send test'))),
      h('div', { class: 'card stack' }, h('h3', null, 'Video storage'), h('dl', { class: 'kv' }, [['Location', s.storage.dir], ['Used', `${s.storage.used_mb} MB`], ['Free', s.storage.free_mb >= 0 ? `${s.storage.free_mb} MB` : 'unknown'], ['Database', s.database]].map(([k, v]) => [h('dt', null, k), h('dd', { class: 'small' }, v)])),
        h('p', { class: 'tiny muted' }, 'Recordings are stored outside the Foyer app and the public web root, and can only be streamed by a signed-in administrator.'),
        (s.persistence?.warnings || []).map((w) => h('div', { class: 'notice danger' }, w))),
      h('div', { class: 'card stack' }, h('h3', null, 'Links'), h('dl', { class: 'kv' }, [['Applicant page', `${s.public_url}/apply`], ['Interview page', `${s.public_url}/join`], ['Session length', `${s.session_hours} h`]].map(([k, v]) => [h('dt', null, k), h('dd', { class: 'small' }, v)])))),
    h('div', { class: 'section-title' }, h('h2', null, 'Audit log')),
    h('div', { class: 'card table-wrap' }, h('table', { class: 'tbl' }, h('tbody', null, audit.items.map((r) => h('tr', null, h('td', { class: 'tiny muted' }, fmtDate(r.ts)), h('td', null, r.action), h('td', { class: 'small muted' }, `${r.target || ''} ${r.detail || ''}`), h('td', { class: 'tiny muted' }, r.ip)))))));
}

init().catch((e) => showLogin(e.message));
