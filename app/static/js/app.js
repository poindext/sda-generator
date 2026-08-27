/* =========================================================
   SDA3 Population Generator — SPA
   Hash-based routing: #dashboard  #new  #templates  #populations/:id
   ========================================================= */

'use strict';

// ── API helpers ────────────────────────────────────────────

const api = {
  async get(path) {
    const r = await fetch('/api' + path);
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async post(path, body) {
    const r = await fetch('/api' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async put(path, body) {
    const r = await fetch('/api' + path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
  async del(path) {
    const r = await fetch('/api' + path, { method: 'DELETE' });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  },
};

// Stream a POST endpoint that returns text/event-stream
async function streamPost(path, body, onEvent, onDone) {
  const r = await fetch('/api' + path, {
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

// Stream a GET SSE endpoint
function streamGet(path, onLine, onDone) {
  const es = new EventSource('/api' + path);
  es.onmessage = (e) => {
    const raw = e.data;
    if (raw.startsWith('[DONE:')) { es.close(); onDone(raw.slice(6, -1)); return; }
    onLine(raw);
  };
  es.onerror = () => { es.close(); onDone('error'); };
  return es;
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

    const running = jobs.filter(j => j.status === 'running').length;
    stats.innerHTML = `
      <div class="stat"><div class="label">Populations</div><div class="value">${pops.length}</div></div>
      <div class="stat"><div class="label">Active Jobs</div><div class="value">${running}</div></div>
      <div class="stat"><div class="label">Total Requested</div><div class="value">${pops.reduce((a,p)=>a+(p.count_requested||0),0).toLocaleString()}</div></div>`;

    if (!pops.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="padding:32px;text-align:center;color:var(--clr-muted)">No populations yet — <a href="#" onclick="navigate('#new');return false">create one</a></td></tr>`;
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

register('new', async () => {
  el('page-new').classList.add('active');
  wizState = { step: 1, sessionId: null, selectedCohorts: [], document: null,
                templateId: null, designJobId: null, genJobId: null };
  await showWizStep(1);
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
  if (step === 3) initGenerateForm();
}

// ── Step 1: Cohort Selection + Chat ────────────────────────

async function initCohortSelection() {
  const grid = el('cohort-grid');
  grid.innerHTML = '<div class="text-muted">Loading cohorts…</div>';
  try {
    const resp = await api.get('/cohorts');
    const cohorts = resp.cohorts;
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
    () => finishAssistantMsg()
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
  _currentBubble = null;
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
      finishAssistantMsg();
      if (evt.ready || ready) {
        show('btn-synthesize');
        show('chat-ready-msg');
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
      finishAssistantMsg();
      wizState.document = evt.document || fullDoc;
      // Auto-advance to step 2 after a brief pause
      setTimeout(() => showWizStep(2), 800);
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

// Override showWizStep for step 2 to trigger doc save
const _origShowStep = showWizStep;

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

function initGenerateForm() {
  if (wizState.templateId) {
    const sel = el('gen-template-select');
    if (sel) sel.value = wizState.templateId;
  }
}

el('gen-form') && document.getElementById('gen-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const templateId = el('gen-template-select').value || wizState.templateId;
  const name = el('gen-pop-name').value.trim();
  const count = parseInt(el('gen-count').value);
  const histMonths = parseInt(el('gen-history-months').value);
  const runQa = el('gen-run-qa').checked;

  if (!templateId || !name) { alert('Template and population name are required.'); return; }

  el('btn-start-gen').disabled = true;

  const resp = await api.post('/jobs', {
    template_id: templateId,
    population_name: name,
    count, history_months: histMonths, run_qa: runQa,
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

  // Chunks
  const chunksWrap = el('result-chunks');
  if (pop.downloadable && pop.chunks?.length) {
    chunksWrap.innerHTML = '<div class="chunk-list">' +
      pop.chunks.map(c => `
        <div class="chunk-item">
          <span class="chunk-name">${c.name}</span>
          <span class="chunk-size">${c.size_mb} MB</span>
          <a class="btn btn-sm btn-secondary" href="/api/populations/${pop.id}/chunks/${c.name}" download>Download</a>
        </div>`).join('') +
      '</div>';
  } else if (pop.run_qa && pop.qa_status !== 'approved') {
    chunksWrap.innerHTML = '<div class="alert alert-warning">Download is blocked until this population passes QA review.</div>';
  } else {
    chunksWrap.innerHTML = '<div class="text-muted text-sm">No chunks available yet.</div>';
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
          <button class="btn btn-sm btn-secondary" onclick="openTemplateEditor('${t.id}')">Edit</button>
          <a class="btn btn-sm btn-secondary" href="/api/templates/${t.id}/export" download>Export</a>
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
  const r = await fetch('/api/templates/import', { method: 'POST', body: fd });
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
    const pop = await api.get(`/populations/${popId}`);
    renderPopDetail(pop);
  } catch (e) {
    el('pop-detail-body').innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
});

function renderPopDetail(pop) {
  el('pop-detail-title').textContent = pop.population_name;
  el('pop-del-btn').dataset.id = pop.id;

  // Status + counts
  el('pop-detail-body').innerHTML = `
    <div class="stat-row">
      <div class="stat"><div class="label">QA Status</div><div class="value" style="font-size:18px">${badge(pop.qa_status)}</div></div>
      <div class="stat"><div class="label">Patients</div><div class="value">${(pop.patient_count||0).toLocaleString()}</div></div>
      <div class="stat"><div class="label">Validation Warnings</div><div class="value">${pop.validation_warnings||0}</div></div>
      <div class="stat"><div class="label">Download</div><div class="value" style="font-size:16px">${pop.downloadable ? '<span class="text-success">Available</span>' : '<span class="text-muted">Locked</span>'}</div></div>
    </div>

    ${pop.qa_issues?.length ? `
    <div class="card mt-16">
      <h3>QA Issues (${pop.qa_issues.length})</h3>
      ${pop.qa_issues.map(i => `
        <div class="qa-issue ${i.severity}">
          <div class="flex items-c gap-8">
            <span class="qi-sev">${i.severity}</span>
            <span class="qi-title">${i.title || i.category}</span>
          </div>
          <div class="qi-evidence">${i.evidence || i.recommendation || ''}</div>
        </div>`).join('')}
    </div>` : pop.run_qa ? '<div class="alert alert-success mt-16">QA passed — no issues found.</div>' : ''}

    <div class="card mt-16">
      <h3>Download Chunks (XML, 10k records each)</h3>
      ${pop.downloadable && pop.chunks?.length
        ? '<div class="chunk-list">' + pop.chunks.map(c => `
            <div class="chunk-item">
              <span class="chunk-name">${c.name}</span>
              <span class="chunk-size">${c.size_mb} MB</span>
              <a class="btn btn-sm btn-secondary" href="/api/populations/${pop.id}/chunks/${c.name}" download>Download</a>
            </div>`).join('') + '</div>'
        : pop.run_qa && pop.qa_status !== 'approved'
          ? '<div class="alert alert-warning">Downloads are locked until QA is approved.</div>'
          : '<div class="text-muted text-sm">No chunks available.</div>'}
    </div>`;
}

el('pop-del-btn') && document.getElementById('pop-del-btn').addEventListener('click', async () => {
  const id = el('pop-del-btn').dataset.id;
  if (!confirm_del('Delete this population? All files will be removed.')) return;
  await api.del(`/populations/${id}`);
  navigate('#dashboard');
});
