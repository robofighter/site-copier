const $ = (s) => document.querySelector(s);

let currentJobId = null;
let timer = null;

const STAGE = {
  queued: 'в очереди',
  crawl: 'сканирование',
  convert: 'сборка статики',
  zip: 'упаковка',
  done: 'готово',
};

// ------------------------------------------------------------------ env

async function checkEnv() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const el = $('#env');
    el.classList.remove('ok', 'bad');
    if (!d.docker) {
      el.classList.add('bad');
      $('#envText').textContent = 'Docker не запущен';
    } else if (!d.imageReady) {
      el.classList.add('bad');
      $('#envText').textContent = 'образ не скачан';
    } else {
      el.classList.add('ok');
      $('#envText').textContent = 'Docker готов';
    }
  } catch {
    $('#env').classList.add('bad');
    $('#envText').textContent = 'сервер недоступен';
  }
}

// ------------------------------------------------------------------ jobs

async function loadJobs() {
  try {
    const r = await fetch('/api/jobs');
    const jobs = await r.json();
    renderJobs(jobs);
  } catch { /* ignore */ }
}

// Перечитать задания с диска — после ручной пересборки копий (правки конвертера)
async function rescanJobs() {
  const btn = $('#rescan');
  if (btn) btn.disabled = true;
  try {
    await fetch('/api/jobs/rescan', { method: 'POST' });
    await loadJobs();
  } catch { /* ignore */ }
  if (btn) btn.disabled = false;
}

function renderJobs(jobs) {
  const box = $('#jobs');
  if (!jobs.length) {
    box.innerHTML = '<p class="empty">Пока пусто</p>';
    return;
  }
  box.innerHTML = jobs.map(jobCard).join('');

  box.querySelectorAll('[data-toggle]').forEach((btn) => {
    btn.onclick = () => {
      const pre = btn.closest('.job').querySelector('pre.log');
      pre.style.display = pre.style.display === 'none' ? 'block' : 'none';
    };
  });

  box.querySelectorAll('[data-preview]').forEach((btn) => {
    btn.onclick = () => openPreview(btn.dataset.preview, btn);
  });
}

async function openPreview(jobId, btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Запускаю…';
  try {
    const r = await fetch(`/api/jobs/${jobId}/open`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'ошибка');
    window.open(d.url, '_blank');
    btn.textContent = d.url.replace('http://127.0.0.1:', ':');
  } catch (err) {
    alert('Не удалось открыть предпросмотр: ' + err.message);
    btn.textContent = old;
  } finally {
    btn.disabled = false;
  }
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function jobCard(j) {
  const running = j.status === 'running' || j.status === 'queued';
  const stage = STAGE[j.stage] || j.stage;

  let meta = '';
  if (j.stats) {
    meta = `<div class="meta">
      <span>${j.stats.pages} страниц</span>
      <span>${j.stats.files} файлов</span>
      <span>${j.stats.sizeMB} MB</span>
      <span>ZIP ${j.stats.zipMB} MB</span>
    </div>`;
  } else if (running && j.pages) {
    meta = `<div class="meta"><span>страниц: ${j.pages}</span></div>`;
  }

  let actions = '';
  if (j.status === 'done') {
    actions = `<div class="actions">
      <a class="primary" href="/api/jobs/${j.id}/download">Скачать ZIP</a>
      <button class="primary alt" data-preview="${j.id}">Открыть предпросмотр</button>
      <button data-toggle>Журнал</button>
    </div>`;
  } else {
    actions = `<div class="actions"><button data-toggle>Журнал</button></div>`;
  }

  return `<div class="job" data-id="${j.id}">
    <div class="jobTop">
      <span class="jobUrl">${esc(j.url)}</span>
      <span class="badge ${j.status}">${esc(stage)}</span>
    </div>
    ${j.error ? `<div class="err">${esc(j.error)}</div>` : ''}
    ${meta}
    ${running ? '<div class="bar"><i></i></div>' : ''}
    ${actions}
    <pre class="log" style="display:none"></pre>
  </div>`;
}

async function loadLog(jobId, pre) {
  try {
    const r = await fetch(`/api/jobs/${jobId}`);
    const d = await r.json();
    pre.textContent = (d.log || []).join('\n');
    pre.scrollTop = pre.scrollHeight;
  } catch { /* ignore */ }
}

// ------------------------------------------------------------------ submit

$('#form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#submit');
  btn.disabled = true;
  btn.textContent = 'Запускаю…';

  const payload = {
    url: $('#url').value.trim(),
    scope: $('#scope').value,
    limit: Number($('#limit').value) || 0,
    workers: Number($('#workers').value) || 2,
    timeLimit: Number($('#timeLimit').value) || 900,
    behaviors: $('#behaviors').value.trim(),
    hideSelectors: $('#hideSelectors').value.split('\n').map(s => s.trim()).filter(Boolean),
  };

  try {
    const r = await fetch('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'ошибка');
    currentJobId = d.id;
    $('#url').value = '';
    await loadJobs();
    startPolling();
  } catch (err) {
    alert('Не удалось запустить: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Скопировать сайт';
  }
});

$('#refresh').onclick = loadJobs;
const rescanBtn = $('#rescan');
if (rescanBtn) rescanBtn.onclick = rescanJobs;

// ------------------------------------------------------------------ polling

function startPolling() {
  if (timer) return;
  timer = setInterval(async () => {
    await loadJobs();
    document.querySelectorAll('.job').forEach((el) => {
      const pre = el.querySelector('pre.log');
      if (pre && pre.style.display !== 'none') loadLog(el.dataset.id, pre);
    });
    const anyRunning = [...document.querySelectorAll('.badge')].some(
      (b) => b.classList.contains('running') || b.classList.contains('queued'));
    if (!anyRunning) {
      clearInterval(timer);
      timer = null;
      checkEnv();
    }
  }, 2000);
}

checkEnv();
loadJobs().then(() => {
  if ([...document.querySelectorAll('.badge')].some(
    (b) => b.classList.contains('running') || b.classList.contains('queued'))) {
    startPolling();
  }
});
setInterval(checkEnv, 15000);
