/* Kosh UI logic — upload, run, poll, download. */

const OPS = {
  optimize: {
    title: 'Optimise files',
    hint: 'Re-compress PNG, ZIP, GZIP and JPEG losslessly — smaller bytes, identical content.',
    note: 'PNG, ZIP, GZIP, JPEG',
    options: [
      { key: 'level', label: 'Effort', type: 'select', value: '2',
        choices: [['1', 'Fast'], ['2', 'Balanced'], ['3', 'Maximum (slow)']],
        hint: 'Maximum tries more filter strategies and more Zopfli passes.' },
      { key: 'strip', label: 'Strip metadata', type: 'check', value: false,
        hint: 'Drop EXIF, text chunks and archive comments.' },
    ],
  },
  pack: {
    title: 'Pack into one container',
    hint: 'Compress files or a folder into a single verified .kosh container.',
    note: 'any files',
    options: [
      { key: 'codec', label: 'Codec', type: 'select', value: 'zstd',
        choices: [['zstd', 'Zstandard'], ['brotli', 'Brotli'], ['lzma', 'LZMA'], ['store', 'No compression']] },
      { key: 'level', label: 'Level', type: 'select', value: '',
        choices: [['', 'Auto'], ['3', '3 · fast'], ['9', '9 · balanced'], ['15', '15 · small'], ['19', '19 · max']],
        hint: 'Ignored for Brotli/LZMA beyond their own ranges.' },
      { key: 'name', label: 'Output name', type: 'text', value: 'archive.kosh' },
    ],
  },
  unpack: {
    title: 'Unpack a container',
    hint: 'Extract a .kosh file and verify every entry as it lands.',
    note: 'one .kosh file',
    options: [],
  },
  split: {
    title: 'Split a file',
    hint: 'Cut a file into parts with a manifest so the join can prove itself.',
    note: 'one file',
    options: [
      { key: 'mode', label: 'Split by', type: 'select', value: 'size',
        choices: [['size', 'Maximum part size'], ['parts', 'Number of parts']] },
      { key: 'size', label: 'Part size', type: 'text', value: '700M', hint: 'e.g. 700M, 4G, 512K' },
      { key: 'parts', label: 'Parts', type: 'number', value: '4' },
    ],
  },
  join: {
    title: 'Join parts',
    hint: 'Upload every part (or the manifest) and Kosh will reassemble and verify it.',
    note: 'all parts + manifest',
    options: [],
  },
  archive: {
    title: 'Create an archive',
    hint: 'Bundle the upload into a format every other machine can open.',
    note: 'any files',
    options: [
      { key: 'name', label: 'Archive name', type: 'text', value: 'archive.zip',
        hint: 'Extension picks the format: .zip, .tar.gz, .tar.zst, .kosh' },
      { key: 'level', label: 'Level', type: 'select', value: '9',
        choices: [['1', '1 · fast'], ['6', '6 · balanced'], ['9', '9 · small'], ['15', '15 · smaller']] },
    ],
  },
  verify: {
    title: 'Verify a file',
    hint: 'Prove a container, split set or archive is intact — without extracting it.',
    note: 'one file',
    options: [
      { key: 'quick', label: 'Structure only', type: 'check', value: false,
        hint: 'Skip decompressing every block; checks the manifest and block bounds.' },
    ],
  },
  inspect: {
    title: 'Inspect a file',
    hint: 'Identify what a file really is and how much room it has left.',
    note: 'one file',
    options: [],
  },
};

const $ = (id) => document.getElementById(id);
const state = { op: 'optimize', files: [], token: null, running: false };

/* ------------------------------------------------------------------ helpers */
const fmt = (n) => {
  if (n === null || n === undefined) return '—';
  if (n < 1000) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n, i = -1;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
};

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function kindOf(name) {
  const ext = name.toLowerCase().split('.').pop();
  if (['png', 'zip', 'gz', 'jpg', 'jpeg', 'kosh'].includes(ext)) return ext;
  return '';
}

/* ------------------------------------------------------------------ options */
function renderOptions() {
  const spec = OPS[state.op];
  $('op-title').textContent = spec.title;
  $('op-hint').textContent = spec.hint;
  const form = $('options');
  form.innerHTML = '';
  if (!spec.options.length) {
    form.hidden = true;
    return;
  }
  form.hidden = false;
  for (const opt of spec.options) {
    const field = document.createElement('div');
    field.className = 'field' + (opt.type === 'check' ? ' check' : '');
    if (opt.type === 'check') {
      field.innerHTML = `<input type="checkbox" id="opt-${opt.key}" ${opt.value ? 'checked' : ''}>
        <label for="opt-${opt.key}">${esc(opt.label)}</label>`;
      if (opt.hint) field.insertAdjacentHTML('beforeend', `<span class="hint">${esc(opt.hint)}</span>`);
    } else {
      field.innerHTML = `<label for="opt-${opt.key}">${esc(opt.label)}</label>`;
      if (opt.type === 'select') {
        const sel = document.createElement('select');
        sel.id = `opt-${opt.key}`;
        for (const [value, label] of opt.choices) {
          const o = document.createElement('option');
          o.value = value; o.textContent = label;
          if (value === opt.value) o.selected = true;
          sel.appendChild(o);
        }
        field.appendChild(sel);
      } else {
        const input = document.createElement('input');
        input.id = `opt-${opt.key}`;
        input.type = opt.type === 'number' ? 'number' : 'text';
        input.value = opt.value ?? '';
        if (opt.type === 'number') input.min = '2';
        field.appendChild(input);
      }
      if (opt.hint) field.insertAdjacentHTML('beforeend', `<span class="hint">${esc(opt.hint)}</span>`);
    }
    form.appendChild(field);
  }
}

function readOptions() {
  const out = {};
  for (const opt of OPS[state.op].options) {
    const el = $(`opt-${opt.key}`);
    if (!el) continue;
    if (opt.type === 'check') out[opt.key] = el.checked;
    else if (opt.type === 'number') out[opt.key] = el.value === '' ? null : Number(el.value);
    else if (opt.type === 'select') out[opt.key] = el.value === '' ? null : el.value;
    else out[opt.key] = el.value.trim();
  }
  return out;
}

/* ------------------------------------------------------------------ files */
function addFiles(list) {
  for (const f of list) {
    const path = f.webkitRelativePath || f.name;
    state.files.push({ file: f, name: path, size: f.size });
  }
  renderFiles();
}

function renderFiles() {
  const box = $('files');
  box.hidden = state.files.length === 0;
  $('file-count').textContent = `${state.files.length} file${state.files.length === 1 ? '' : 's'}`;
  $('file-size').textContent = fmt(state.files.reduce((a, f) => a + f.size, 0));
  const list = $('file-list');
  list.innerHTML = '';
  for (const [index, item] of state.files.entries()) {
    const li = document.createElement('li');
    const kind = kindOf(item.name);
    li.innerHTML = `<span class="kind">${kind || 'file'}</span>
      <span class="name" title="${esc(item.name)}">${esc(item.name)}</span>
      <span class="size">${fmt(item.size)}</span>`;
    li.title = 'click to remove';
    li.style.cursor = 'pointer';
    li.onclick = () => { state.files.splice(index, 1); renderFiles(); };
    list.appendChild(li);
  }
  updateRunState();
}

function updateRunState() {
  const ready = state.files.length > 0 && !state.running;
  $('run').disabled = !ready;
  const spec = OPS[state.op];
  $('run-note').textContent = state.running
    ? 'working…'
    : state.files.length
      ? (spec.note ? `expects ${spec.note}` : '')
      : 'Add a file to start';
}

/* ------------------------------------------------------------------ upload */
function upload() {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    const paths = [];
    for (const item of state.files) {
      form.append('files', item.file, item.name.split('/').pop());
      paths.push(item.name);
    }
    form.append('paths', JSON.stringify(paths));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = (e.loaded / e.total) * 100;
        showProgress('uploading', pct, 'sending to local server');
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else reject(new Error(xhr.responseText || `upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error('upload failed'));
    xhr.send(form);
  });
}

/* ------------------------------------------------------------------ progress */
function showProgress(label, pct, message) {
  $('progress-wrap').hidden = false;
  $('progress-msg').textContent = message || label;
  $('progress-pct').textContent = `${Math.round(pct)}%`;
  $('bar').style.width = `${Math.max(2, Math.min(100, pct))}%`;
}

function hideProgress() {
  $('progress-wrap').hidden = true;
  $('bar').style.width = '0%';
}

/* ------------------------------------------------------------------ run */
async function run() {
  if (state.running) return;
  state.running = true;
  $('run').classList.add('is-busy');
  $('run').querySelector('.run-label').textContent = 'Working';
  $('result').hidden = true;
  $('error').hidden = true;
  updateRunState();

  try {
    const params = readOptions();
    const uploaded = await upload();
    state.token = uploaded.token;
    params.files = uploaded.files.map((f) => f.name);

    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: uploaded.token, op: state.op, params }),
    });
    if (!response.ok) throw new Error(await response.text());
    const { id } = await response.json();
    await poll(id);
  } catch (err) {
    showError(err.message || String(err));
  } finally {
    state.running = false;
    $('run').classList.remove('is-busy');
    $('run').querySelector('.run-label').textContent = 'Run';
    updateRunState();
    hideProgress();
  }
}

async function poll(id) {
  for (;;) {
    const res = await fetch(`/api/job/${id}`);
    if (!res.ok) throw new Error('job disappeared');
    const job = await res.json();
    if (job.status === 'running' || job.status === 'queued') {
      showProgress(job.op, job.progress * 100, job.message || 'working…');
      await new Promise((r) => setTimeout(r, 320));
      continue;
    }
    if (job.status === 'error') {
      showError(job.error || 'operation failed');
      return;
    }
    hideProgress();
    renderResult(job);
    return;
  }
}

/* ------------------------------------------------------------------ result */
const STAT_LABELS = {
  before: 'Before', after: 'After', saved: 'Saved', percent: 'Reduction',
  raw_size: 'Original', stored_size: 'Stored', ratio: 'Ratio',
  entries: 'Files', parts: 'Parts', part_size: 'Part size', total: 'Size',
  bytes: 'Bytes', verified: 'Verified', codec: 'Codec', format: 'Format',
  kind: 'Type', sha256: 'SHA-256', seconds: 'Seconds', output: 'Output',
  ok: 'Intact', manifest_verified: 'Manifest',
};

function renderResult(job) {
  const box = $('result');
  const r = job.result || {};
  const parts = [];

  parts.push(`<div class="result-head"><span class="ok"></span>
    <span>${esc(OPS[job.op]?.title || job.op)} — finished in ${job.seconds}s</span></div>`);

  const stats = [];
  const push = (k, v, cls) => {
    const label = STAT_LABELS[k] || k;
    stats.push(`<div class="stat"><div class="k">${esc(label)}</div>
      <div class="v ${cls || ''}">${v}</div></div>`);
  };

  if (r.before !== undefined) {
    push('before', fmt(r.before));
    push('after', fmt(r.after));
    push('saved', `−${fmt(r.saved)}`, 'good');
    push('percent', `${r.percent}%`, r.percent > 0 ? 'good' : '');
  } else if (r.raw_size !== undefined && r.stored_size !== undefined) {
    push('raw_size', fmt(r.raw_size));
    push('stored_size', fmt(r.stored_size));
    const ratio = r.ratio !== undefined ? r.ratio : r.stored_size / (r.raw_size || 1);
    push('ratio', `${(ratio * 100).toFixed(1)}%`, ratio < 0.8 ? 'good' : 'warm');
    if (r.entries !== undefined) push('entries', r.entries);
    if (r.codec) push('codec', esc(r.codec));
    if (r.format) push('format', esc(r.format));
  } else {
    for (const [k, v] of Object.entries(r)) {
      if (k === 'files' || k === 'listing') continue;
      if (typeof v === 'object') continue;
      if (k === 'sha256') push(k, `<span style="font-size:11px">${esc(String(v).slice(0, 16))}…</span>`);
      else if (k === 'total' || k === 'part_size' || k === 'bytes' || k === 'stored_size') push(k, fmt(v));
      else if (k === 'verified' || k === 'ok' || k === 'manifest_verified') push(k, v ? 'yes' : 'no', v ? 'good' : 'warm');
      else push(k, esc(String(v)));
    }
  }
  parts.push(`<div class="stats">${stats.join('')}</div>`);

  if (r.files && r.files.length) {
    const rows = r.files.map((f) => {
      const delta = f.saved > 0 ? `<span style="color:var(--jade)">−${fmt(f.saved)}</span>` : '<span class="muted">no change</span>';
      return `<tr><td>${esc(f.path.split('/').pop())}</td>
        <td>${fmt(f.before)} → ${fmt(f.after)}</td><td>${delta}</td></tr>`;
    }).join('');
    parts.push(`<div class="listing"><table>${rows}</table></div>`);
  }

  if (r.listing && r.listing.length) {
    const rows = r.listing.slice(0, 200).map((e) => `<tr><td>${esc(e.name)}</td>
      <td>${fmt(e.size || 0)}</td></tr>`).join('');
    parts.push(`<div class="listing"><table>${rows}</table></div>`);
  }

  if (job.files && job.files.length) {
    const links = job.files.map((f) => `<a class="download" href="${esc(f.url)}" download>
      <svg viewBox="0 0 24 24" width="18" height="18"><path d="M12 4v11m0 0l-4-4m4 4l4-4M5 19h14"/></svg>
      <span>${esc(f.name)}</span><span class="size">${fmt(f.size)}</span></a>`).join('');
    parts.push(`<div class="downloads">${links}</div>`);
  }

  box.innerHTML = parts.join('');
  box.hidden = false;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function showError(message) {
  const box = $('error');
  box.textContent = message;
  box.hidden = false;
}

/* ------------------------------------------------------------------ wiring */
for (const button of document.querySelectorAll('.op')) {
  button.addEventListener('click', () => {
    document.querySelectorAll('.op').forEach((b) => b.classList.remove('is-active'));
    button.classList.add('is-active');
    state.op = button.dataset.op;
    $('result').hidden = true;
    $('error').hidden = true;
    renderOptions();
    updateRunState();
  });
}

$('browse').addEventListener('click', () => $('picker').click());
$('browse-folder').addEventListener('click', () => $('folder-picker').click());
$('picker').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
$('folder-picker').addEventListener('change', (e) => { addFiles(e.target.files); e.target.value = ''; });
$('clear').addEventListener('click', () => { state.files = []; renderFiles(); });
$('run').addEventListener('click', run);

const drop = $('drop');
drop.addEventListener('click', (e) => { if (e.target.closest('button')) return; $('picker').click(); });
drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('picker').click(); } });
for (const type of ['dragenter', 'dragover']) {
  drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add('is-over'); });
}
for (const type of ['dragleave', 'drop']) {
  drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.remove('is-over'); });
}
drop.addEventListener('drop', (e) => {
  if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
});

document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('drop', (e) => e.preventDefault());

renderOptions();
updateRunState();
fetch('/api/health').then((r) => r.json()).then((d) => {
  $('version').textContent = `v${d.version}`;
}).catch(() => {});
