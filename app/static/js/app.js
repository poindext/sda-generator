/* =========================================================
   Genesis — Synthetic Patient Population Generator
   Hash-based routing: #dashboard  #new  #templates  #populations/:id
   ========================================================= */

'use strict';

// Base path for all API calls — auto-detected from the URL so the app works
// at any mount point (root or a sub-path such as /genesis).
const API_BASE = window.location.pathname.replace(/\/$/, '');

// ── API helpers ────────────────────────────────────────────

const api = {
  async get(path) {
    const r = await fetch(API_BASE + '/api' + path);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(API_BASE + '/api' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async put(path, body) {
    const r = await fetch(API_BASE + '/api' + path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async del(path) {
    const r = await fetch(API_BASE + '/api' + path, { method: 'DELETE' });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
};

// Stream a POST endpoint that returns text/event-stream
async function streamPost(path, body, onEvent, onDone) {
  const r = await fetch(API_BASE + '/api' + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== null ? JSON.stringify(body) : null,
  });
  if (!r.ok) { onDone({ error: await r.text() }); return; }

  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      try {
        const evt = JSON.parse(raw);
        if (evt.type === 'done' || evt.done) { onDone(evt); return; }
        onEvent(evt);
      } catch (_) {
        // plain text SSE (design/generator log lines)
        onEvent({ type: 'line', content: raw });
        if (raw.startsWith('[DONE:')) { onDone({ status: raw.slice(6, -1) }); return; }
      }
    }
  }
  onDone({});
}

// Poll a job progress endpoint (IIS-compatible replacement for SSE EventSource).
// path is like /jobs/{id}/stream — the /lines endpoint is derived from it.
function streamGet(path, onLine, onDone) {
  const linesPath = path.replace('/stream', '/lines');
  let since = 0;
  let stopped = false;

  async function poll() {
    if (stopped) return;
    try {
      const resp = await api.get(linesPath + '?since=' + since);
      for (const line of resp.lines) onLine(line);
      since += resp.lines.length;
      if (['completed', 'failed', 'cancelled'].includes(resp.status)) {
        onDone(resp.status);
        return;
      }
    } catch (e) {
      console.error('streamGet poll error', e);
      onDone('error');
      return;
    }
    setTimeout(poll, 1000);
  }

  poll();
  return { close() { stopped = true; } };
}

// ── Router ─────────────────────────────────────────────────

const routes = {};
function register(hash, fn) { routes[hash] = fn; }

function navigate(hash) {
  history.pushState(null, '', hash || '#dashboard');
  render(hash || '#dashboard');
}

function render(hash) {
  const [base, param] = (hash || '#dashboard').replace('#', '').split('/');
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#sidebar nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.page === base);
  });
  const fn = routes[base];
  if (fn) fn(param);
}

window.addEventListener('popstate', () => render(location.hash));

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('#sidebar nav a[data-page]').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      navigate('#' + a.dataset.page);
    });
  });
  render(location.hash || '#dashboard');
});

// ── Helpers ────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }
function show(id) { el(id)?.classList.remove('hidden'); }
function hide(id) { el(id)?.classList.add('hidden'); }

function badge(status) {
  return `<span class="badge badge-${status}">${status.replace('_', ' ')}</span>`;
}

function fmt_date(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit'
  });
}

function fmt_size(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}

function confirm_del(msg) { return window.confirm(msg); }

// Append a line to a .log-box element
function logLine(box, text) {
  const div = document.createElement('div');
  if (text.includes('[OK]'))   div.className = 'log-ok';
  if (text.includes('ERROR') || text.includes('FAIL')) div.className = 'log-err';
  if (text.includes('WARN') || text.includes('WARNING')) div.className = 'log-warn';
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// Parse "[OK] N/M" progress lines
function parseProgress(text) {
  const m = text.match(/\[OK\]\s+(\d+)\/(\d+)/);
  return m ? { done: +m[1], total: +m[2] } : null;
}

// Enforce QA auto-fix rules for large populations
const QA_AUTO_FIX_LIMIT = 5000;
function _qaLargePopCheck(countEl, qaCheckEl, autoFixEl, autoFixRowEl) {
  const count = parseInt(countEl?.value) || 0;
  const qaOn  = qaCheckEl?.checked;
  if (!autoFixRowEl) return;
  autoFixRowEl.style.display = qaOn ? '' : 'none';

  let warn = autoFixRowEl.nextElementSibling?.classList.contains('qa-large-warn')
    ? autoFixRowEl.nextElementSibling : null;
  if (!warn) {
    warn = document.createElement('div');
    warn.className = 'qa-large-warn text-sm';
    warn.style.cssText = 'padding:4px 0 0 24px;color:#b45309;display:none';
    autoFixRowEl.after(warn);
  }

  if (qaOn && count > QA_AUTO_FIX_LIMIT) {
    autoFixEl.checked = false;
    autoFixEl.disabled = true;
    warn.textContent = `Auto-fix disabled for populations over ${QA_AUTO_FIX_LIMIT.toLocaleString()} patients. QA will report issues without regenerating.`;
    warn.style.display = '';
  } else {
    autoFixEl.disabled = false;
    warn.style.display = 'none';
  }
}

// =========================================================
// PAGE: DASHBOARD
// =========================================================

register('dashboard', async () => {
  el('page-dashboard').classList.add('active');
  const stats = el('dash-stats');
  const tbody = el('pop-table-body');
  stats.innerHTML = '<div class="text-muted">Loading…</div>';
  tbody.innerHTML = '<tr><td colspan="6" class="text-muted" style="padding:16px">Loading…</td></tr>';

  try {
    const [popResp, jobResp] = await Promise.all([
      api.get('/populations'),
      api.get('/jobs'),
    ]);
    const pops = popResp.populations;
    const jobs = jobResp.jobs;

    const popDirs = new Set(pops.map(p => p.output_dir || p.id));
    const running = jobs.filter(j => j.status === 'running' &&
      (!j.params?.output_dir || pops.some(p => j.params.output_dir.endsWith(p.id)))).length;
    stats.innerHTML = `
      <div class="stat"><div class="label">Populations</div><div class="value">${pops.length}</div></div>
      <div class="stat"><div class="label">Active Jobs</div><div class="value">${running}</div></div>
      <div class="stat"><div class="label">Total Requested</div><div class="value">${pops.reduce((a,p)=>a+(p.count_requested||0),0).toLocaleString()}</div></div>`;

    if (!pops.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="padding:32px;text-align:center;color:var(--clr-muted)">No populations yet — <a href="#" onclick="navigate('#generate');return false">create one</a></td></tr>`;
      return;
    }

    tbody.innerHTML = pops.map(p => `
      <tr>
        <td><a href="#" onclick="navigate('#populations/${p.id}');return false" style="color:var(--clr-primary);font-weight:500">${p.population_name}</a></td>
        <td>${p.count_requested.toLocaleString()}</td>
        <td>${badge(p.qa_status)}</td>
        <td>${p.downloadable ? '<span class="text-success">✓ Ready</span>' : '<span class="text-muted">—</span>'}</td>
        <td class="text-muted">${fmt_date(p.modified)}</td>
        <td>
          <button class="btn btn-sm btn-secondary" onclick="navigate('#populations/${p.id}')">View</button>
          <button class="btn btn-sm btn-danger" onclick="deletePop('${p.id}')">Delete</button>
        </td>
      </tr>`).join('');
  } catch (e) {
    stats.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
});

window.deletePop = async (id) => {
  if (!confirm_del('Delete this population? This cannot be undone.')) return;
  await api.del(`/populations/${id}`);
  navigate('#dashboard');
};

// =========================================================
// PAGE: NEW POPULATION (wizard)
// =========================================================

let wizState = {
  step: 1,
  sessionId: null,
  selectedCohorts: [],
  document: null,
  templateId: null,
  designJobId: null,
  genJobId: null,
};

let _pendingTemplateId = null;

window.generateFromTemplate = (templateId) => {
  _pendingTemplateId = templateId;
  navigate('#new');
};

register('new', async () => {
  el('page-new').classList.add('active');
  const presetTemplate = _pendingTemplateId;
  _pendingTemplateId = null;
  wizState = { step: 1, sessionId: null, selectedCohorts: [], document: null,
               templateId: presetTemplate, designJobId: null, genJobId: null };
  await showWizStep(presetTemplate ? 3 : 1);
});

async function showWizStep(step) {
  wizState.step = step;
  [1,2,3,4].forEach(s => {
    el(`wiz-step-${s}`)?.classList.toggle('hidden', s !== step);
    const ind = el(`step-ind-${s}`);
    if (ind) {
      ind.classList.toggle('active', s === step);
      ind.classList.toggle('done', s < step);
    }
  });

  if (step === 1) await initCohortSelection();
  if (step === 2) await initStep2();
  if (step === 3) await initGenerateForm();
  if (step === 4) {
    const logBox = el('gen-log');
    if (logBox) logBox.innerHTML = '';
    const bar = el('gen-progress-fill');
    if (bar) bar.style.width = '0%';
    hide('gen-results');
  }
}

async function initStep2() {
  el('txt-preview').value = wizState.document || '';
  el('txt-preview-2').value = wizState.document || '';

  if (!wizState.templateId) {
    const name = prompt('Give this population a name (used for file names):', 'my_population');
    if (!name) return;
    wizState.templateId = name.toLowerCase().replace(/[^a-z0-9_-]/g, '_');
    try {
      await api.post('/templates/description', {
        name: wizState.templateId,
        document: wizState.document,
      });
    } catch (e) {
      console.error('Could not save description:', e);
    }
  }
}

// ── Step 1: Cohort Selection + Chat ────────────────────────

async function initCohortSelection() {
  const grid = el('cohort-grid');
  grid.innerHTML = '<div class="text-muted">Loading cohorts…</div>';
  try {
    const resp = await api.get('/cohorts');
    const cohorts = (resp.cohorts || []).sort((a, b) => a.name.localeCompare(b.name));
    wizState.selectedCohorts = [];

    grid.innerHTML = cohorts.map(c => `
      <div class="cohort-card" data-id="${c.id}" data-name="${c.name}" onclick="toggleCohort(this)">
        <span class="cchk hidden">✓</span>
        <div class="cname">${c.name}</div>
        <div class="ckw">${(c.keywords || []).slice(0,3).join(', ')}</div>
      </div>`).join('');
  } catch (_) {
    grid.innerHTML = '<div class="text-muted text-sm">Could not load catalog cohorts</div>';
  }
  el('chat-messages').innerHTML = '';
  hide('btn-synthesize');
  hide('chat-ready-msg');
}

window.toggleCohort = (card) => {
  card.classList.toggle('sel');
  card.querySelector('.cchk').classList.toggle('hidden', !card.classList.contains('sel'));
  const id = card.dataset.name;
  if (card.classList.contains('sel')) {
    wizState.selectedCohorts.push(id);
  } else {
    wizState.selectedCohorts = wizState.selectedCohorts.filter(x => x !== id);
  }
};

el('btn-start-interview') && document.getElementById('btn-start-interview').addEventListener('click', async () => {
  const resp = await api.post('/wizard/sessions', { selected_cohorts: wizState.selectedCohorts });
  wizState.sessionId = resp.session_id;
  el('btn-start-interview').disabled = true;
  el('chat-input-row').classList.remove('hidden');

  // Stream opening message
  appendTyping();
  await streamPost(`/wizard/sessions/${wizState.sessionId}/opening`, null,
    (evt) => {
      if (evt.type === 'token') appendToken(evt.content);
    },
    (evt) => {
      if (evt.error) {
        appendErrorMsg('Could not start interview: ' + evt.error +
          ' — check that OPENAI_API_KEY is configured on the server.');
        el('btn-start-interview').disabled = false;
      } else {
        finishAssistantMsg();
      }
    }
  );
});

function appendTyping() {
  const div = document.createElement('div');
  div.className = 'msg msg-typing';
  div.id = 'typing-bubble';
  div.textContent = 'Typing…';
  el('chat-messages').appendChild(div);
  el('chat-messages').scrollTop = el('chat-messages').scrollHeight;
}

let _currentBubble = null;
function appendToken(text) {
  const typing = document.getElementById('typing-bubble');
  if (typing) {
    typing.remove();
    _currentBubble = null;
  }
  if (!_currentBubble) {
    _currentBubble = document.createElement('div');
    _currentBubble.className = 'msg msg-assistant';
    el('chat-messages').appendChild(_currentBubble);
  }
  _currentBubble.textContent += text;
  el('chat-messages').scrollTop = el('chat-messages').scrollHeight;
}

function finishAssistantMsg() {
  document.getElementById('typing-bubble')?.remove();
  _currentBubble = null;
}

function appendErrorMsg(text) {
  document.getElementById('typing-bubble')?.remove();
  _currentBubble = null;
  const div = document.createElement('div');
  div.className = 'msg msg-assistant';
  div.style.cssText = 'background:#fef2f2;color:#991b1b';
  div.textContent = '⚠ ' + text;
  el('chat-messages').appendChild(div);
  el('chat-messages').scrollTop = el('chat-messages').scrollHeight;
}

function appendUserMsg(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-user';
  div.textContent = text;
  el('chat-messages').appendChild(div);
  el('chat-messages').scrollTop = el('chat-messages').scrollHeight;
}

el('btn-send-chat') && document.getElementById('btn-send-chat').addEventListener('click', sendChatMessage);
el('chat-input') && document.getElementById('chat-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
});

async function sendChatMessage() {
  const input = el('chat-input');
  const text = input.value.trim();
  if (!text || !wizState.sessionId) return;
  input.value = '';

  appendUserMsg(text);
  appendTyping();

  let ready = false;
  await streamPost(`/wizard/sessions/${wizState.sessionId}/message`, { content: text },
    (evt) => {
      if (evt.type === 'token') appendToken(evt.content);
      if (evt.type === 'done') ready = evt.ready;
    },
    (evt) => {
      if (evt.error) {
        appendErrorMsg('Error: ' + evt.error);
      } else {
        finishAssistantMsg();
        if (evt.ready || ready) {
          show('btn-synthesize');
          show('chat-ready-msg');
        }
      }
    }
  );
}

el('btn-synthesize') && document.getElementById('btn-synthesize').addEventListener('click', async () => {
  el('btn-synthesize').disabled = true;
  appendTyping();

  let fullDoc = '';
  await streamPost(`/wizard/sessions/${wizState.sessionId}/synthesize`, null,
    (evt) => {
      if (evt.type === 'token') { appendToken(evt.content); fullDoc += evt.content; }
    },
    (evt) => {
      if (evt.error) {
        appendErrorMsg('Synthesis failed: ' + evt.error);
        el('btn-synthesize').disabled = false;
      } else {
        finishAssistantMsg();
        wizState.document = evt.document || fullDoc;
        setTimeout(() => showWizStep(2), 600);
      }
    }
  );
});

// ── Step 2: Review document + Design ──────────────────────

async function showStep2Document() {
  el('txt-preview').value = wizState.document || '';

  // Ask for a population name for saving
  const name = prompt('Give this population a name (used for file names):', 'my_population');
  if (!name) return;
  wizState.templateId = name.toLowerCase().replace(/[^a-z0-9_-]/g, '_');

  // Save the .txt description
  await api.post('/templates/description', { name: wizState.templateId, document: wizState.document });
}

document.addEventListener('DOMContentLoaded', () => {
  el('page-new')?.addEventListener('show-step-2', showStep2Document);
});

el('btn-design-template') && document.getElementById('btn-design-template').addEventListener('click', async () => {
  if (!wizState.templateId) return;
  el('btn-design-template').disabled = true;
  el('design-log-wrap').classList.remove('hidden');
  const logBox = el('design-log');
  logBox.innerHTML = '';

  const resp = await api.post(`/templates/${wizState.templateId}/design`, {});
  wizState.designJobId = resp.job_id;

  streamGet(`/jobs/${wizState.designJobId}/stream`,
    (line) => logLine(logBox, line),
    async (status) => {
      if (status === 'completed') {
        logLine(logBox, '✓ Template design complete.');
        // Load the template into the editor
        await loadTemplateEditor(wizState.templateId);
        show('wiz-editor-section');
        el('btn-proceed-to-generate').disabled = false;
      } else {
        logLine(logBox, `✗ Design failed: ${status}`);
      }
    }
  );
});

// CodeMirror editor instance
let _cmEditor = null;

async function loadTemplateEditor(templateId) {
  try {
    const resp = await api.get(`/templates/${templateId}`);
    const json = JSON.stringify(resp.content, null, 2);
    if (_cmEditor) {
      _cmEditor.dispatch({ changes: { from: 0, to: _cmEditor.state.doc.length, insert: json } });
    } else {
      // Fallback: plain textarea
      el('template-json-ta').value = json;
      show('template-json-ta');
    }
  } catch (e) {
    console.error('Could not load template', e);
  }
}

el('btn-save-template') && document.getElementById('btn-save-template').addEventListener('click', async () => {
  try {
    let content;
    if (_cmEditor) {
      content = JSON.parse(_cmEditor.state.doc.toString());
    } else {
      content = JSON.parse(el('template-json-ta').value);
    }
    await api.put(`/templates/${wizState.templateId}`, { content });
    el('btn-save-template').textContent = '✓ Saved';
    setTimeout(() => { el('btn-save-template').textContent = 'Save Template'; }, 2000);
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
});

el('btn-proceed-to-generate') && document.getElementById('btn-proceed-to-generate').addEventListener('click', () => showWizStep(3));

// ── Step 3: Generate ───────────────────────────────────────

async function initGenerateForm() {
  el('btn-start-gen').disabled = false;
  const sel = el('gen-template-select');
  sel.innerHTML = '<option value="">— select a template —</option>';
  try {
    const resp = await api.get('/templates');
    resp.templates.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = t.name || t.id;
      sel.appendChild(opt);
    });
  } catch (_) {}
  if (wizState.templateId) sel.value = wizState.templateId;
}

el('gen-run-qa') && el('gen-run-qa').addEventListener('change', () => {
  _qaLargePopCheck(el('gen-count'), el('gen-run-qa'), el('gen-auto-fix'), el('auto-fix-row'));
});
el('gen-count') && el('gen-count').addEventListener('input', () => {
  _qaLargePopCheck(el('gen-count'), el('gen-run-qa'), el('gen-auto-fix'), el('auto-fix-row'));
});

el('gen-form') && document.getElementById('gen-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const templateId = el('gen-template-select').value || wizState.templateId;
  const name = el('gen-pop-name').value.trim();
  const count = parseInt(el('gen-count').value);
  const histMonths = parseInt(el('gen-history-months').value);
  const runQa = el('gen-run-qa').checked;
  const autoFix = runQa && (el('gen-auto-fix') ? el('gen-auto-fix').checked : true);

  if (!templateId || !name) { alert('Template and population name are required.'); return; }

  el('btn-start-gen').disabled = true;

  const resp = await api.post('/jobs', {
    template_id: templateId,
    population_name: name,
    count, history_months: histMonths, run_qa: runQa, auto_fix: autoFix,
  });
  wizState.genJobId = resp.job_id;
  await showWizStep(4);
  startGenProgress(wizState.genJobId, runQa);
});

// ── Step 4: Progress + Results ─────────────────────────────

function startGenProgress(jobId, runQa) {
  const logBox = el('gen-log');
  const bar = el('gen-progress-fill');
  logBox.innerHTML = '';
  bar.style.width = '0%';

  streamGet(`/jobs/${jobId}/stream`,
    (line) => {
      logLine(logBox, line);
      const p = parseProgress(line);
      if (p && p.total > 0) {
        bar.style.width = Math.min(100, Math.round(p.done / p.total * 100)) + '%';
      }
      if (line.includes('── Starting QA')) bar.style.width = '95%';
    },
    async (status) => {
      bar.style.width = '100%';
      if (status === 'completed') {
        logLine(logBox, '✓ Complete');
        const ind4 = el('step-ind-4');
        if (ind4) { ind4.classList.remove('active'); ind4.classList.add('done'); }
        await loadPopResults(wizState.genJobId);
      } else {
        logLine(logBox, `✗ Job ended with status: ${status}`);
      }
    }
  );
}

async function loadPopResults(jobId) {
  try {
    const job = await api.get(`/jobs/${jobId}`);
    const outDir = job.params?.output_dir;
    if (!outDir) return;
    const popId = outDir.split('/').pop();
    const pop = await api.get(`/populations/${popId}`);
    renderPopResults(pop);
    show('gen-results');
  } catch (e) {
    console.error('Could not load results', e);
  }
}

function renderPopResults(pop) {
  el('result-pop-name').textContent = pop.population_name;
  el('result-qa-status').innerHTML = badge(pop.qa_status);
  el('result-patient-count').textContent = (pop.patient_count || 0).toLocaleString();

  // QA issues
  const issuesWrap = el('result-qa-issues');
  if (pop.qa_issues?.length) {
    issuesWrap.innerHTML = pop.qa_issues.map(i => `
      <div class="qa-issue ${i.severity}">
        <div class="flex items-c gap-8">
          <span class="qi-sev">${i.severity}</span>
          <span class="qi-title">${i.title}</span>
        </div>
        <div class="qi-evidence">${i.evidence || ''}</div>
      </div>`).join('');
  } else if (pop.run_qa) {
    issuesWrap.innerHTML = '<div class="alert alert-success">No QA issues found — population approved.</div>';
  } else {
    issuesWrap.innerHTML = '<div class="text-muted text-sm">QA was not run.</div>';
  }

  // Chunks / fix panel
  const chunksWrap = el('result-chunks');
  if (pop.downloadable && pop.chunks?.length) {
    let html = '<div class="chunk-list">' +
      pop.chunks.map(c => `
        <div class="chunk-item">
          <span class="chunk-name">${c.name}</span>
          <span class="chunk-size">${c.size_mb} MB</span>
          <a class="btn btn-sm btn-secondary" href="${API_BASE}/api/populations/${pop.id}/chunks/${c.name}" download>Download</a>
        </div>`).join('') +
      '</div>';
    if (pop.qa_status === 'needs_review') {
      html += `
      <div class="mt-16">
        <div class="flex items-c gap-16" style="justify-content:space-between">
          <div>
            <strong>Fix QA Issues</strong>
            <p class="text-muted text-sm" style="margin:4px 0 0">Let the AI fix the template and regenerate, or re-run QA if you believe the findings are false positives.</p>
          </div>
          <div class="flex gap-8">
            <button id="btn-reqa-results" class="btn btn-secondary" data-popid="${pop.id}">Re-run QA</button>
            <button id="btn-fix-results" class="btn btn-primary" data-popid="${pop.id}">Fix &amp; Regenerate</button>
          </div>
        </div>
        <div id="fix-results-log-wrap" class="hidden mt-12">
          <div id="fix-results-log" class="log-box"></div>
        </div>
      </div>`;
    }
    chunksWrap.innerHTML = html;
  } else if (pop.run_qa && pop.qa_status === 'needs_review') {
    chunksWrap.innerHTML = `
      <div class="flex items-c gap-16" style="justify-content:space-between">
        <div>
          <strong>Resolve QA Issues</strong>
          <p class="text-muted text-sm" style="margin:4px 0 0">Re-run QA if you believe the findings are false positives, or let the AI fix the template and regenerate.</p>
        </div>
        <div class="flex gap-8">
          <button id="btn-reqa-results" class="btn btn-secondary" data-popid="${pop.id}">Re-run QA</button>
          <button id="btn-fix-results" class="btn btn-primary" data-popid="${pop.id}">Fix &amp; Regenerate</button>
        </div>
      </div>
      <div id="fix-results-log-wrap" class="hidden mt-12">
        <div id="fix-results-log" class="log-box"></div>
      </div>`;

    el('btn-fix-results') && el('btn-fix-results').addEventListener('click', async () => {
      const popId = el('btn-fix-results').dataset.popid;
      el('btn-fix-results').disabled = true;
      el('btn-fix-results').textContent = 'Running…';
      show('fix-results-log-wrap');
      const logBox = el('fix-results-log');
      logBox.innerHTML = '';
      try {
        const resp = await api.post(`/populations/${popId}/fix`, {});
        streamGet(`/jobs/${resp.job_id}/stream`,
          (line) => logLine(logBox, line),
          async (status) => {
            if (status === 'completed') {
              logLine(logBox, '');
              logLine(logBox, '✓ Done — reloading results…');
              const updated = await api.get(`/populations/${popId}`);
              renderPopResults(updated);
            } else {
              logLine(logBox, `Fix job ended with status: ${status}`);
              el('btn-fix-results') && (el('btn-fix-results').disabled = false);
              el('btn-fix-results') && (el('btn-fix-results').textContent = 'Fix & Regenerate');
            }
          }
        );
      } catch (e) {
        logLine(logBox, `Error: ${e.message}`);
        el('btn-fix-results') && (el('btn-fix-results').disabled = false);
        el('btn-fix-results') && (el('btn-fix-results').textContent = 'Fix & Regenerate');
      }
    });

    el('btn-reqa-results') && el('btn-reqa-results').addEventListener('click', async () => {
      const popId = el('btn-reqa-results').dataset.popid;
      el('btn-reqa-results').disabled = true;
      el('btn-reqa-results').textContent = 'Running…';
      el('btn-fix-results').disabled = true;
      show('fix-results-log-wrap');
      const logBox = el('fix-results-log');
      logBox.innerHTML = '';
      try {
        const resp = await api.post(`/populations/${popId}/reqa`, {});
        streamGet(`/jobs/${resp.job_id}/stream`,
          (line) => logLine(logBox, line),
          async (status) => {
            logLine(logBox, '');
            logLine(logBox, '✓ Done — reloading results…');
            const updated = await api.get(`/populations/${popId}`);
            renderPopResults(updated);
          }
        );
      } catch (e) {
        logLine(logBox, `Error: ${e.message}`);
        el('btn-reqa-results') && (el('btn-reqa-results').disabled = false);
        el('btn-reqa-results') && (el('btn-reqa-results').textContent = 'Re-run QA');
        el('btn-fix-results') && (el('btn-fix-results').disabled = false);
      }
    });
  } else {
    chunksWrap.innerHTML = '<div class="text-muted text-sm">No chunks available yet.</div>';
  }
}

// =========================================================
// =========================================================
// PAGE: GENERATE (quick flow — pick a template and go)
// =========================================================

register('generate', async () => {
  el('page-generate').classList.add('active');

  // Reset to form view each time the page loads
  hide('gen2-progress-card');
  hide('gen2-results');
  show('gen2-form-card');
  el('btn-start-gen2').disabled = false;

  // Populate template dropdown
  try {
    const resp = await api.get('/templates');
    const sel = el('gen2-template-select');
    sel.innerHTML = '<option value="">— select a template —</option>';
    (resp.templates || []).forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = t.name || t.id;
      sel.appendChild(opt);
    });
  } catch (e) {
    console.error('Could not load templates', e);
  }

  // Attach listeners only once
  if (el('gen2-form')._init) return;
  el('gen2-form')._init = true;

  el('gen2-run-qa').addEventListener('change', () => {
    _qaLargePopCheck(el('gen2-count'), el('gen2-run-qa'), el('gen2-auto-fix'), el('gen2-auto-fix-row'));
  });
  el('gen2-count').addEventListener('input', () => {
    _qaLargePopCheck(el('gen2-count'), el('gen2-run-qa'), el('gen2-auto-fix'), el('gen2-auto-fix-row'));
  });

  el('gen2-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const templateId = el('gen2-template-select').value;
    const name = el('gen2-pop-name').value.trim();
    const count = parseInt(el('gen2-count').value);
    const histMonths = parseInt(el('gen2-history-months').value);
    const runQa = el('gen2-run-qa').checked;
    const autoFix = runQa && el('gen2-auto-fix').checked;

    if (!templateId || !name) { alert('Template and population name are required.'); return; }

    el('btn-start-gen2').disabled = true;
    hide('gen2-form-card');
    show('gen2-progress-card');
    hide('gen2-results');

    const resp = await api.post('/jobs', {
      template_id: templateId,
      population_name: name,
      count, history_months: histMonths, run_qa: runQa, auto_fix: autoFix,
    });

    const logBox = el('gen2-log');
    const bar = el('gen2-progress-fill');
    logBox.innerHTML = '';
    bar.style.width = '0%';

    streamGet(`/jobs/${resp.job_id}/stream`,
      (line) => {
        logLine(logBox, line);
        const p = parseProgress(line);
        if (p && p.total > 0) bar.style.width = Math.min(100, Math.round(p.done / p.total * 100)) + '%';
        if (line.includes('── Starting QA') || line.includes('── Round')) bar.style.width = '95%';
      },
      async (status) => {
        bar.style.width = '100%';
        if (status === 'completed') {
          logLine(logBox, '✓ Complete — loading results…');
          const popId = resp.pop_id ||
            (resp.output_dir || '').replace(/\\/g, '/').split('/').filter(Boolean).pop();
          setTimeout(() => navigate(`#populations/${popId}`), 800);
        } else {
          logLine(logBox, `✗ Job ended with status: ${status}`);
        }
      }
    );
  });
});

function renderGen2Results(pop) {
  el('gen2-result-pop-name').textContent = pop.population_name;
  el('gen2-result-qa-status').innerHTML = badge(pop.qa_status);
  el('gen2-result-patient-count').textContent = (pop.patient_count || 0).toLocaleString();

  const issuesWrap = el('gen2-result-qa-issues');
  if (pop.qa_issues?.length) {
    issuesWrap.innerHTML = pop.qa_issues.map(i => `
      <div class="qa-issue ${i.severity}">
        <div class="flex items-c gap-8">
          <span class="qi-sev">${i.severity}</span>
          <span class="qi-title">${i.title}</span>
        </div>
        <div class="qi-evidence">${i.evidence || ''}</div>
      </div>`).join('');
  } else if (pop.run_qa) {
    issuesWrap.innerHTML = '<div class="alert alert-success">No QA issues found — population approved.</div>';
  } else {
    issuesWrap.innerHTML = '<div class="text-muted text-sm">QA was not run.</div>';
  }

  const chunksWrap = el('gen2-result-chunks');
  if (pop.downloadable && pop.chunks?.length) {
    let html = '<div class="chunk-list">' +
      pop.chunks.map(c => `
        <div class="chunk-item">
          <span class="chunk-name">${c.name}</span>
          <span class="chunk-size">${c.size_mb} MB</span>
          <a class="btn btn-sm btn-secondary" href="${API_BASE}/api/populations/${pop.id}/chunks/${c.name}" download>Download</a>
        </div>`).join('') +
      '</div>';
    if (pop.qa_status === 'needs_review') {
      html += `
      <div style="margin-top:16px">
        <strong>QA issues remain after auto-fix.</strong>
        <p class="text-muted text-sm" style="margin:4px 0 8px">Download the population or view the full detail page to re-run fix rounds.</p>
        <button class="btn btn-secondary" onclick="navigate('#populations/${pop.id}')">View Detail</button>
      </div>`;
    }
    chunksWrap.innerHTML = html;
  } else {
    chunksWrap.innerHTML = '';
  }
}

// =========================================================
// PAGE: TEMPLATES
// =========================================================

register('templates', async () => {
  el('page-templates').classList.add('active');
  await loadTemplateList();
});

async function loadTemplateList() {
  const tbody = el('tmpl-table-body');
  tbody.innerHTML = '<tr><td colspan="5" class="text-muted" style="padding:16px">Loading…</td></tr>';
  try {
    const resp = await api.get('/templates');
    const templates = resp.templates;
    if (!templates.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="padding:32px;text-align:center;color:var(--clr-muted)">No templates yet</td></tr>';
      return;
    }
    tbody.innerHTML = templates.map(t => `
      <tr>
        <td><strong>${t.name || t.id}</strong></td>
        <td>${t.state || '—'}</td>
        <td>${(t.total_patients||0).toLocaleString()}</td>
        <td class="text-muted">${fmt_date(t.modified)}</td>
        <td>
          <button class="btn btn-sm btn-primary" onclick="generateFromTemplate('${t.id}')">Generate →</button>
          <button class="btn btn-sm btn-secondary" onclick="openTemplatePreview('${t.id}')">View</button>
          <button class="btn btn-sm btn-secondary" onclick="openTemplateEditor('${t.id}')">Edit</button>
          <a class="btn btn-sm btn-secondary" href="${API_BASE}/api/templates/${t.id}/export" download>Export</a>
          <button class="btn btn-sm btn-danger" onclick="deleteTemplate('${t.id}')">Delete</button>
        </td>
      </tr>`).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="alert alert-error">${e.message}</div></td></tr>`;
  }
}

window.deleteTemplate = async (id) => {
  if (!confirm_del('Delete this template?')) return;
  await api.del(`/templates/${id}`);
  await loadTemplateList();
};

window.openTemplateEditor = async (id) => {
  show('tmpl-editor-panel');
  el('tmpl-editor-title').textContent = id;
  el('tmpl-save-btn').dataset.id = id;
  try {
    const resp = await api.get(`/templates/${id}`);
    el('tmpl-editor-ta').value = JSON.stringify(resp.content, null, 2);
  } catch (e) {
    el('tmpl-editor-ta').value = '{}';
  }
};

el('tmpl-save-btn') && document.getElementById('tmpl-save-btn').addEventListener('click', async () => {
  const id = el('tmpl-save-btn').dataset.id;
  try {
    const content = JSON.parse(el('tmpl-editor-ta').value);
    await api.put(`/templates/${id}`, { content });
    el('tmpl-save-btn').textContent = '✓ Saved';
    setTimeout(() => { el('tmpl-save-btn').textContent = 'Save'; }, 2000);
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
});

el('tmpl-import-btn') && document.getElementById('tmpl-import-btn').addEventListener('click', () => {
  el('tmpl-import-file').click();
});

el('tmpl-import-file') && document.getElementById('tmpl-import-file').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(API_BASE + '/api/templates/import', { method: 'POST', body: fd });
  if (r.ok) { await loadTemplateList(); }
  else { alert('Import failed'); }
  e.target.value = '';
});

// =========================================================
// PAGE: POPULATION DETAIL
// =========================================================

register('populations', async (popId) => {
  el('page-population').classList.add('active');
  if (!popId) { navigate('#dashboard'); return; }
  el('pop-detail-title').textContent = 'Loading…';
  try {
    const [pop, stats, csvResp] = await Promise.all([
      api.get(`/populations/${popId}`),
      api.get(`/populations/${popId}/stats`).catch(() => null),
      api.get(`/populations/${popId}/csvs`).catch(() => null),
    ]);
    pop.csvFiles = csvResp?.files || [];
    renderPopDetail(pop, stats);
  } catch (e) {
    el('pop-detail-body').innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
});

function renderPopDetail(pop, stats) {
  el('pop-detail-title').textContent = pop.population_name;
  el('pop-del-btn').dataset.id = pop.id;

  const sp = stats?.patients  || {};
  const se = stats?.encounters || {};
  const sm = stats?.medications || {};
  const sl = stats?.labs       || {};

  // ── Stat cards ──────────────────────────────────────────────
  const statCards = `
    <div class="stat-row">
      <div class="stat">
        <div class="label">QA Status</div>
        <div class="value" style="font-size:18px">${badge(pop.qa_status)}</div>
      </div>
      <div class="stat">
        <div class="label">Patients</div>
        <div class="value">${(sp.total || pop.patient_count || 0).toLocaleString()}</div>
      </div>
      <div class="stat">
        <div class="label">Encounters</div>
        <div class="value">${(se.total || 0).toLocaleString()}</div>
      </div>
      <div class="stat">
        <div class="label">Medications</div>
        <div class="value">${(sm.total_events || 0).toLocaleString()}</div>
      </div>
      <div class="stat">
        <div class="label">Lab Results</div>
        <div class="value">${(sl.total_results || 0).toLocaleString()}</div>
      </div>
    </div>`;

  // ── Secondary stat row (rates / percentages) ─────────────────
  let rateCards = '';
  if (stats) {
    rateCards = `
      <div class="stat-row">
        <div class="stat">
          <div class="label">Avg Encounters / Patient</div>
          <div class="value">${se.avg_per_patient || '—'}</div>
        </div>
        <div class="stat">
          <div class="label">Acute Encounters</div>
          <div class="value">${se.acute_pct != null ? se.acute_pct + '%' : '—'}</div>
        </div>
        <div class="stat">
          <div class="label">Abnormal Labs</div>
          <div class="value">${sl.abnormal_pct != null ? sl.abnormal_pct + '%' : '—'}</div>
        </div>
        <div class="stat">
          <div class="label">Multi-Facility Patients</div>
          <div class="value">${sp.multi_facility_pct != null ? sp.multi_facility_pct + '%' : '—'}</div>
        </div>
        ${se.date_range?.earliest ? `
        <div class="stat">
          <div class="label">Date Range</div>
          <div class="value" style="font-size:13px;margin-top:8px">${se.date_range.earliest} – ${se.date_range.latest}</div>
        </div>` : ''}
      </div>`;
  }

  // ── Condition prevalence ─────────────────────────────────────
  let condSection = '';
  if (sp.conditions && sp.total) {
    const total = sp.total;
    const entries = Object.entries(sp.conditions)
      .sort((a, b) => b[1] - a[1])
      .filter(([, v]) => v > 0);
    const maxPct = entries[0] ? entries[0][1] / total * 100 : 1;
    condSection = `
      <div class="card mt-16">
        <h3>Condition Prevalence</h3>
        ${entries.map(([label, count]) => {
          const pct = count / total * 100;
          const barW = Math.round(pct / maxPct * 100);
          return `<div class="dist-row no-sub">
            <span class="dist-label">${label}</span>
            <div class="dist-bar-wrap"><div class="dist-bar" style="width:${barW}%"></div></div>
            <span class="dist-pct">${pct.toFixed(1)}%</span>
          </div>`;
        }).join('')}
      </div>`;
  }

  // ── Demographics ─────────────────────────────────────────────
  let demoSection = '';
  if (sp.age_buckets || sp.sex || sp.race) {
    const total = sp.total || 1;
    const ageEntries  = Object.entries(sp.age_buckets || {});
    const sexEntries  = Object.entries(sp.sex || {});
    const raceEntries = Object.entries(sp.race || {}).slice(0, 6);
    const narrow = (entries, divisor) => entries.map(([k, v]) => {
      const barW = divisor ? Math.round(v / divisor * 100) : 0;
      return `<div class="dist-row narrow">
        <span class="dist-label">${k}</span>
        <div class="dist-bar-wrap"><div class="dist-bar" style="width:${barW}%"></div></div>
        <span class="dist-pct">${Math.round(v / total * 100)}%</span>
      </div>`;
    }).join('');
    const maxAge  = Math.max(...ageEntries.map(([,v]) => v),  1);
    const maxSex  = Math.max(...sexEntries.map(([,v]) => v),  1);
    const maxRace = Math.max(...raceEntries.map(([,v]) => v), 1);
    demoSection = `
      <div class="card mt-16">
        <h3>Demographics</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 32px">
          <div>
            <div class="text-muted text-sm" style="font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Age</div>
            ${narrow(ageEntries, maxAge)}
          </div>
          <div>
            <div class="text-muted text-sm" style="font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Sex</div>
            ${narrow(sexEntries, maxSex)}
          </div>
          <div>
            <div class="text-muted text-sm" style="font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Race</div>
            ${narrow(raceEntries, maxRace)}
          </div>
        </div>
      </div>`;
  }

  // ── Top diagnoses ────────────────────────────────────────────
  let dxSection = '';
  if (se.top_diagnoses?.length) {
    const maxC = se.top_diagnoses[0].count || 1;
    dxSection = `
      <div class="card mt-16">
        <h3>Top Diagnoses</h3>
        ${se.top_diagnoses.map(d => {
          const label = d.dx.length > 40 ? d.dx.slice(0, 38) + '…' : d.dx;
          return `<div class="dist-row no-sub" title="${d.dx}">
            <span class="dist-label">${label}</span>
            <div class="dist-bar-wrap"><div class="dist-bar" style="width:${Math.round(d.count/maxC*100)}%"></div></div>
            <span class="dist-pct">${d.count.toLocaleString()}</span>
          </div>`;
        }).join('')}
      </div>`;
  }

  // ── QA issues ────────────────────────────────────────────────
  const qaSection = pop.qa_issues?.length
    ? `<div class="card mt-16">
        <h3>QA Issues (${pop.qa_issues.length})</h3>
        ${pop.qa_issues.map(i => `
          <div class="qa-issue ${i.severity}">
            <div class="flex items-c gap-8">
              <span class="qi-sev">${i.severity}</span>
              <span class="qi-title">${i.title || i.category}</span>
            </div>
            <div class="qi-evidence">${i.evidence || i.recommendation || ''}</div>
          </div>`).join('')}
      </div>`
    : pop.run_qa ? '<div class="alert alert-success mt-16">QA passed — no issues found.</div>' : '';

  // ── Fix panel ────────────────────────────────────────────────
  const fixPanel = pop.qa_status === 'needs_review' ? `
    <div class="card mt-16" id="fix-panel">
      <div class="flex items-c gap-16" style="justify-content:space-between">
        <div>
          <h3>Auto-Fix &amp; Regenerate</h3>
          <p class="text-muted text-sm">The AI applies fixes to the template, regenerates, and re-runs QA — up to 5 rounds.</p>
        </div>
        <div class="flex gap-8">
          <button id="btn-reqa-pop" class="btn btn-secondary" data-popid="${pop.id}">Re-run QA</button>
          <button id="btn-fix-pop" class="btn btn-primary" data-popid="${pop.id}">Fix &amp; Regenerate</button>
        </div>
      </div>
      <div id="fix-log-wrap" class="hidden mt-12">
        <div id="fix-log" class="log-box"></div>
      </div>
      <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--clr-border);display:flex;align-items:center;justify-content:space-between">
        <div>
          <strong style="font-size:13px">Override QA</strong>
          <p class="text-muted text-sm" style="margin:2px 0 0">Skip QA approval and unlock download as-is. Issues remain on record.</p>
        </div>
        <button id="btn-override-qa" class="btn btn-secondary" data-popid="${pop.id}" style="white-space:nowrap">Override / Allow Download</button>
      </div>
    </div>` : '';

  // ── CSV downloads ─────────────────────────────────────────────
  const csvSection = pop.csvFiles?.length
    ? `<div class="card mt-16">
        <div class="flex items-c gap-16" style="justify-content:space-between;margin-bottom:12px">
          <h3 style="margin-bottom:0">CSV Data Files <span class="text-muted text-sm" style="font-weight:400">(for database import)</span></h3>
          <a class="btn btn-sm btn-primary" href="${API_BASE}/api/populations/${pop.id}/csvs.zip" download>⬇ Download All (ZIP)</a>
        </div>
        <div class="chunk-list">${
          pop.csvFiles.map(f => `
            <div class="chunk-item">
              <span class="chunk-name">${f.name}</span>
              <span class="chunk-size">${f.size_kb} KB</span>
              <a class="btn btn-sm btn-secondary" href="${API_BASE}/api/populations/${pop.id}/csvs/${f.name}" download>Download</a>
            </div>`).join('')
        }</div>
      </div>`
    : '';

  // ── Download manager ─────────────────────────────────────────
  const chunkRows = pop.downloadable && pop.chunks?.length
    ? '<div class="chunk-list">' +
      pop.chunks.map(c => `
        <div class="chunk-item">
          <span class="chunk-name">${c.name}</span>
          <span class="chunk-size">${c.size_mb} MB</span>
          <a class="btn btn-sm btn-secondary" href="${API_BASE}/api/populations/${pop.id}/chunks/${c.name}" download>Download</a>
        </div>`).join('') +
      '</div>'
    : pop.run_qa && pop.qa_status !== 'approved'
      ? '<div class="alert alert-warning">Downloads are locked until QA is approved.</div>'
      : '<div class="text-muted text-sm">No chunks available.</div>';

  const dlAllBtn = pop.downloadable && pop.chunks?.length > 1
    ? `<button id="btn-dl-all" class="btn btn-sm btn-primary" data-popid="${pop.id}">⬇ Download All (${pop.chunks.length} chunks)</button>`
    : '';

  const dlSection = `
    <div class="card mt-16">
      <div class="flex items-c gap-16" style="justify-content:space-between;margin-bottom:12px">
        <h3 style="margin-bottom:0">Download Chunks <span class="text-muted text-sm" style="font-weight:400">(SDA3 XML, 10k patients/zip)</span></h3>
        ${dlAllBtn}
      </div>
      <div id="dl-all-status" class="hidden text-muted text-sm mb-8"></div>
      ${chunkRows}
    </div>`;

  // ── Render ───────────────────────────────────────────────────
  el('pop-detail-body').innerHTML =
    statCards + rateCards + condSection + demoSection + dxSection +
    qaSection + fixPanel + csvSection + dlSection;

  // Download All button handler
  el('btn-dl-all') && el('btn-dl-all').addEventListener('click', async () => {
    const chunks = pop.chunks;
    if (!chunks?.length) return;
    const btn = el('btn-dl-all');
    const statusEl = el('dl-all-status');
    btn.disabled = true;
    statusEl.classList.remove('hidden');
    for (let i = 0; i < chunks.length; i++) {
      statusEl.textContent = `Downloading chunk ${i + 1} of ${chunks.length}: ${chunks[i].name}…`;
      const a = document.createElement('a');
      a.href = `${API_BASE}/api/populations/${pop.id}/chunks/${chunks[i].name}`;
      a.download = chunks[i].name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      if (i < chunks.length - 1) await new Promise(r => setTimeout(r, 1500));
    }
    statusEl.textContent = `All ${chunks.length} chunk(s) queued for download.`;
    btn.disabled = false;
  });

  // Fix & Regenerate handler
  el('btn-fix-pop') && el('btn-fix-pop').addEventListener('click', async () => {
    const popId = el('btn-fix-pop').dataset.popid;
    el('btn-fix-pop').disabled = true;
    el('btn-fix-pop').textContent = 'Running…';
    show('fix-log-wrap');
    const logBox = el('fix-log');
    logBox.innerHTML = '';
    try {
      const resp = await api.post(`/populations/${popId}/fix`, {});
      streamGet(`/jobs/${resp.job_id}/stream`,
        (line) => logLine(logBox, line),
        async (status) => {
          if (status === 'completed') {
            logLine(logBox, '');
            logLine(logBox, '✓ Done — refreshing…');
            setTimeout(() => navigate(`#populations/${popId}`), 1500);
          } else {
            logLine(logBox, `Fix job ended with status: ${status}`);
            el('btn-fix-pop').disabled = false;
            el('btn-fix-pop').textContent = 'Fix & Regenerate';
          }
        }
      );
    } catch (e) {
      logLine(logBox, `Error starting fix: ${e.message}`);
      el('btn-fix-pop').disabled = false;
      el('btn-fix-pop').textContent = 'Fix & Regenerate';
    }
  });

  // Re-run QA handler
  el('btn-reqa-pop') && el('btn-reqa-pop').addEventListener('click', async () => {
    const popId = el('btn-reqa-pop').dataset.popid;
    el('btn-reqa-pop').disabled = true;
    el('btn-reqa-pop').textContent = 'Running QA…';
    show('fix-log-wrap');
    const logBox = el('fix-log');
    logBox.innerHTML = '';
    try {
      const resp = await api.post(`/populations/${popId}/reqa`, {});
      streamGet(`/jobs/${resp.job_id}/stream`,
        (line) => logLine(logBox, line),
        async (status) => {
          if (status === 'completed') {
            logLine(logBox, '');
            logLine(logBox, '✓ Done — refreshing…');
            setTimeout(() => navigate(`#populations/${popId}`), 1500);
          } else {
            logLine(logBox, `QA job ended with status: ${status}`);
            el('btn-reqa-pop').disabled = false;
            el('btn-reqa-pop').textContent = 'Re-run QA';
          }
        }
      );
    } catch (e) {
      logLine(logBox, `Error starting QA: ${e.message}`);
      el('btn-reqa-pop').disabled = false;
      el('btn-reqa-pop').textContent = 'Re-run QA';
    }
  });

  // Override QA handler
  el('btn-override-qa') && el('btn-override-qa').addEventListener('click', async () => {
    const popId = el('btn-override-qa').dataset.popid;
    if (!window.confirm('Override QA and unlock this population for download?\n\nQA issues will remain on record but download will be allowed.')) return;
    el('btn-override-qa').disabled = true;
    el('btn-override-qa').textContent = 'Overriding…';
    try {
      await api.post(`/populations/${popId}/override-qa`, {});
      setTimeout(() => navigate(`#populations/${popId}`), 500);
    } catch (e) {
      alert('Override failed: ' + e.message);
      el('btn-override-qa').disabled = false;
      el('btn-override-qa').textContent = 'Override / Allow Download';
    }
  });
}

el('pop-del-btn') && document.getElementById('pop-del-btn').addEventListener('click', async () => {
  const id = el('pop-del-btn').dataset.id;
  if (!confirm_del('Delete this population? All files will be removed.')) return;
  await api.del(`/populations/${id}`);
  navigate('#dashboard');
});

// =========================================================
// TEMPLATE PREVIEW MODAL
// =========================================================

window.openTemplatePreview = async (templateId) => {
  try {
    const resp = await api.get(`/templates/${templateId}`);
    renderTemplatePreview(resp.content);
    el('tmpl-preview-modal').classList.remove('hidden');
    document.body.style.overflow = 'hidden';
  } catch (e) {
    alert('Could not load template: ' + e.message);
  }
};

window.closeTemplatePreview = () => {
  el('tmpl-preview-modal').classList.add('hidden');
  document.body.style.overflow = '';
};

function renderTemplatePreview(t) {
  const meta = t.meta || {};
  el('tmpl-preview-title').textContent = meta.name || 'Template';
  el('tmpl-preview-subtitle').textContent =
    [meta.state, meta.total_patients ? (meta.total_patients).toLocaleString() + ' patients' : '',
     meta.history_months ? meta.history_months + ' months history' : '']
    .filter(Boolean).join(' · ');

  const body = el('tmpl-preview-body');
  body.innerHTML = '';

  // Helper: percentage formatter
  const pct = (w) => Math.round((w || 0) * 100) + '%';

  // Helper: render a distribution section with bars
  const distSection = (title, items, labelFn, subFn) => {
    const total = items.reduce((s, i) => s + (i.weight || 0), 0) || 1;
    const rowClass = subFn ? 'dist-row' : 'dist-row no-sub';
    return `<div class="preview-section">
      <h3>${title}</h3>
      ${items.map(i => {
        const w = (i.weight || 0) / total;
        return `<div class="${rowClass}">
          <div class="dist-label">${labelFn(i)}</div>
          ${subFn ? `<div class="dist-sub">${subFn(i)}</div>` : ''}
          <div class="dist-bar-wrap"><div class="dist-bar" style="width:${Math.round(w*100)}%"></div></div>
          <div class="dist-pct">${Math.round(w * 100)}%</div>
        </div>`;
      }).join('')}
    </div>`;
  };

  let html = '';

  // ── Geography ──────────────────────────────────────────
  const locs = (t.geography || {}).locations || [];
  if (locs.length) {
    html += distSection('Geography — Counties', locs,
      i => i.county + (i.region ? ` <span class="text-muted" style="font-size:11px">(${i.region})</span>` : ''),
      i => `<span style="text-transform:capitalize">${i.rurality || ''}</span>`
    );
  }

  // ── Demographics ───────────────────────────────────────
  const dem = t.demographics || {};
  if (dem.age_distribution?.length) {
    html += distSection('Age Distribution', dem.age_distribution,
      i => i.label || `${i.min}–${i.max}`,
      i => `Ages ${i.min}–${i.max}`
    );
  }
  if (dem.sex_distribution?.length) {
    html += distSection('Sex Distribution', dem.sex_distribution,
      i => i.sex === 'F' ? 'Female' : i.sex === 'M' ? 'Male' : i.sex,
      null
    );
  }
  if (dem.race_distribution?.length) {
    html += distSection('Race / Ethnicity', dem.race_distribution,
      i => i.race_description || i.race_code,
      i => i.ethnicity_description || ''
    );
  }
  if (dem.insurance_distribution?.length) {
    // Try to resolve insurance plan names
    const planMap = {};
    (t.insurance_plans || []).forEach(p => { planMap[p.code] = p.name; });
    html += distSection('Insurance', dem.insurance_distribution,
      i => planMap[i.plan_code] || i.plan_code,
      null
    );
  }

  // ── Facilities ─────────────────────────────────────────
  const facs = t.facilities || [];
  if (facs.length) {
    html += `<div class="preview-section"><h3>Facilities (${facs.length})</h3>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--clr-border);text-align:left">
          <th style="padding:6px 8px">Name</th>
          <th style="padding:6px 8px">Health System</th>
          <th style="padding:6px 8px">City</th>
          <th style="padding:6px 8px;text-align:right">Weight</th>
        </tr></thead>
        <tbody>${facs.map(f => `
          <tr style="border-bottom:1px solid #f1f5f9">
            <td style="padding:6px 8px;font-weight:500">${f.name}</td>
            <td style="padding:6px 8px;color:var(--clr-muted)">${f.health_system_name || ''}</td>
            <td style="padding:6px 8px;color:var(--clr-muted)">${f.city || ''}</td>
            <td style="padding:6px 8px;text-align:right">${pct(f.weight)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  }

  // ── Multi-facility ─────────────────────────────────────
  const mf = t.multi_facility || {};
  if (mf.enabled && mf.distribution) {
    const d = mf.distribution;
    html += `<div class="preview-section"><h3>Multi-Facility Distribution</h3>
      <div style="display:flex;gap:24px;font-size:13px">
        <div>Single facility <strong>${pct(d.one_facility_pct)}</strong></div>
        <div>Two facilities <strong>${pct(d.two_facility_pct)}</strong></div>
        <div>Three+ facilities <strong>${pct(d.three_plus_facility_pct)}</strong></div>
      </div>
    </div>`;
  }

  // ── Cohorts ────────────────────────────────────────────
  const cohorts = t.cohorts || [];
  if (cohorts.length) {
    html += `<div class="preview-section"><h3>Cohorts (${cohorts.length})</h3>`;
    cohorts.forEach(c => {
      const sexLabel = c.sex_bias === 'F' ? 'Female only' : c.sex_bias === 'M' ? 'Male only' : 'All sexes';
      const ageLabel = `Ages ${c.min_age ?? 0}–${c.max_age ?? 100}`;
      const enc = c.encounter_pattern || {};
      const typeW = enc.encounter_type_weights || {};
      const encTypes = Object.entries(typeW)
        .map(([k,v]) => `${k === 'O' ? 'Outpatient' : k === 'E' ? 'ED' : k === 'I' ? 'Inpatient' : k} ${pct(v)}`)
        .join(' · ');

      const dxList = [...(c.diagnoses || []), ...(c.comorbidities || [])]
        .slice(0, 6)
        .map(d => `<li>${d.description || d.code}</li>`).join('');
      const medList = (c.medications || []).slice(0, 5)
        .map(m => `<li>${m.drug_description || m.drug_code}</li>`).join('');
      const labList = (c.labs || []).slice(0, 5)
        .map(l => `<li>${l.order_description || l.order_code}</li>`).join('');

      html += `<div class="cohort-preview-card">
        <div class="cohort-preview-header">
          <span class="cohort-preview-name">${c.name}</span>
          <span class="cohort-preview-pct">${pct(c.weight)} of population</span>
        </div>
        <div class="cohort-preview-meta">${ageLabel} · ${sexLabel}</div>
        ${c.description ? `<div class="cohort-preview-desc">${c.description}</div>` : ''}
        <div class="cohort-preview-grid">
          ${dxList ? `<div class="cohort-preview-block">
            <div class="block-title">Diagnoses</div><ul>${dxList}</ul></div>` : ''}
          ${medList ? `<div class="cohort-preview-block">
            <div class="block-title">Medications</div><ul>${medList}</ul></div>` : ''}
          ${labList ? `<div class="cohort-preview-block">
            <div class="block-title">Lab Orders</div><ul>${labList}</ul></div>` : ''}
          ${enc.encounters_per_year ? `<div class="cohort-preview-block">
            <div class="block-title">Encounter Pattern</div>
            <ul>
              <li>${enc.encounters_per_year} visits/year</li>
              ${encTypes ? `<li style="color:var(--clr-muted)">${encTypes}</li>` : ''}
              ${enc.lab_encounter_rate ? `<li>${pct(enc.lab_encounter_rate)} lab rate</li>` : ''}
            </ul></div>` : ''}
        </div>
      </div>`;
    });
    html += `</div>`;
  }

  body.innerHTML = html;
}
