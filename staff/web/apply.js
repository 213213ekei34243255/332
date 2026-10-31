import { h, mount, $, request } from './ui.js';

const EDU = { high_school: 'High school', diploma: 'Diploma', bachelor: "Bachelor's degree", master: "Master's degree", doctorate: 'Doctorate' };
let positions = [];
const els = {
  org: $('#org'), list: $('#positions'), intro: $('#intro'), formSection: $('#form-section'), form: $('#apply-form'),
  title: $('#form-title'), req: $('#req-box'), err: $('#form-error'), pid: $('#position_id'), done: $('#done'),
  doneMsg: $('#done-msg'), submit: $('#submit-btn'), spin: $('#submit-spinner'), consent: $('#consent'),
};

function requirementsText(p) {
  const r = p.requirements || {};
  const parts = [];
  if (r.min_experience_years > 0) parts.push(`${r.min_experience_years}+ years of experience`);
  if (r.min_education) parts.push(`${EDU[r.min_education] || r.min_education} or higher`);
  if (r.must_have_skills?.length) parts.push(`skills: ${r.must_have_skills.join(', ')}`);
  return parts;
}

function roleCard(p) {
  const meta = [p.department, p.location, p.employment_type, p.seniority].filter(Boolean);
  return h('article', { class: 'card role' },
    h('h3', null, p.title),
    h('div', { class: 'meta' }, meta.map((m) => h('span', { class: 'badge' }, m))),
    p.description && h('p', { class: 'desc' }, p.description),
    p.required_skills?.length ? h('div', { class: 'chips' }, p.required_skills.slice(0, 6).map((s) => h('span', { class: 'chip' }, s))) : null,
    h('div', { class: 'row between' },
      h('span', { class: 'tiny muted' }, `~${p.duration_minutes} min AI interview`),
      h('button', { class: 'btn small', type: 'button', onclick: () => openForm(p.id) }, 'Apply')));
}

function openForm(id) {
  const p = positions.find((x) => x.id === id);
  if (!p) return;
  els.pid.value = p.id;
  els.title.textContent = `Apply: ${p.title}`;
  const reqs = requirementsText(p);
  if (reqs.length || p.requirements?.notes) {
    els.req.hidden = false;
    mount(els.req, h('strong', null, 'What we are looking for: '), reqs.join(' · '), p.requirements.notes ? h('div', { class: 'small' }, p.requirements.notes) : null);
  } else els.req.hidden = true;
  els.err.hidden = true;
  els.intro.hidden = true; els.list.hidden = true; els.done.hidden = true; els.formSection.hidden = false;
  history.replaceState(null, '', `?position=${encodeURIComponent(p.id)}`);
  window.scrollTo({ top: 0 });
  $('#full_name').focus();
}

function showList() {
  els.formSection.hidden = true; els.done.hidden = true; els.intro.hidden = false; els.list.hidden = false;
  history.replaceState(null, '', location.pathname);
}

function fail(msg) { els.err.hidden = false; els.err.textContent = msg; els.err.scrollIntoView({ behavior: 'smooth', block: 'center' }); }

async function load() {
  try {
    const data = await request('/api/recruit/positions');
    positions = data.positions;
    if (data.org) { els.org.textContent = data.org; document.title = `Careers at ${data.org}`; }
    if (!positions.length) {
      mount(els.list, h('div', { class: 'card center muted' }, 'There are no open positions right now. Please check back soon.'));
      return;
    }
    mount(els.list, positions.map(roleCard));
    const wanted = new URLSearchParams(location.search).get('position');
    if (wanted && positions.some((p) => p.id === wanted)) openForm(wanted);
  } catch (e) {
    mount(els.list, h('div', { class: 'notice danger' }, e.message));
  }
}

els.form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  els.err.hidden = true;
  const fd = new FormData(els.form);
  if (String(fd.get('full_name')).trim().length < 2) return fail('Please enter your full name.');
  if (!/^\S+@\S+\.\S+$/.test(String(fd.get('email')).trim())) return fail('Please enter a valid email address.');
  if (fd.get('experience_years') === '' || fd.get('experience_years') === null) return fail('Please enter your years of experience (0 if none).');
  const resume = fd.get('resume');
  if (resume && resume.size > 5 * 1024 * 1024) return fail('The resume is larger than 5 MB.');
  if (resume && resume.size === 0) fd.delete('resume');
  if (!els.consent.checked) return fail('Please tick the consent box to continue.');
  els.submit.disabled = true; els.spin.hidden = false;
  try {
    const out = await request('/api/recruit/apply', { method: 'POST', form: fd, timeout: 120000 });
    els.formSection.hidden = true; els.done.hidden = false;
    els.doneMsg.textContent = out.message || 'Thank you for applying.';
    els.form.reset();
    window.scrollTo({ top: 0 });
  } catch (e) {
    fail(e.message);
  } finally {
    els.submit.disabled = false; els.spin.hidden = true;
  }
});

$('#back-btn').addEventListener('click', showList);
$('#again-btn').addEventListener('click', showList);
load();
