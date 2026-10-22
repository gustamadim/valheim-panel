'use strict';

const csrf = document.body.dataset.csrf || '';
const me = document.body.dataset.user || '';
const myRole = document.body.dataset.role || 'user';
let caps = new Set();

/* ---------- helpers ---------- */
const $ = sel => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const can = cap => caps.has(cap);

function humanBytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function humanDur(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

function ageOf(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

function clockOf(f) {
  if (f == null) return '—';
  const mins = f * 24 * 60;
  const h = Math.floor(mins / 60), m = Math.floor(mins % 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function whenOf(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const now = Date.now();
  const age = (now - d.getTime()) / 1000;
  if (age < 86400) return `${d.toLocaleTimeString()} · ${humanDur(age)} ago`;
  return d.toLocaleString();
}

async function post(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({...body, csrf})
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('unauthorized'); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

/* ---------- session / capability bootstrap ---------- */
async function loadSession() {
  try {
    const r = await fetch('/api/v1/session', {cache: 'no-store'});
    if (r.status === 401) { location.href = '/login'; return; }
    const d = await r.json();
    caps = new Set(d.capabilities || []);
    $('#role-badge').textContent = d.role;
    $('#role-badge').className = 'rolebadge role-' + d.role;
    if (can('users.manage')) $('#tab-users').hidden = false;
    if (can('audit.view')) $('#tab-audit').hidden = false;
    if (can('server.restart') || can('lists.edit') || can('world.delete')) {
      $('#tab-manage').hidden = false;
    }
    if (can('view.players') || can('player.broadcast') || can('view.logs')) {
      $('#tab-console').hidden = false;
    }
    if (can('view.players')) $('#tab-world').hidden = false;
    if (can('view.players')) $('#tab-map').hidden = false;
    if (can('view.mods')) $('#tab-mods').hidden = false;
    if (can('player.broadcast')) $('#qc-broadcast').hidden = false;
    if (can('player.message')) $('#qc-say').hidden = false;
    if (can('server.restart')) $('#qc-save').hidden = false;
    if (can('rcon.freeform')) $('#freeform-card').hidden = false;
    // Server-control buttons follow capability, not role name.
    if (can('server.restart')) $('#btn-restart').hidden = false;
    if (can('server.stop')) $('#btn-stop').hidden = false;
    if (can('server.start')) $('#btn-start').hidden = false;
    if (can('server.restart')) $('#btn-save').hidden = false;
    if (can('backup.create')) $('#backup-actions').hidden = false;
  } catch (_) { /* leave defaults */ }
}

/* ---------- tabs ---------- */
let currentView = 'dash';
let logStarted = false;

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tab));
    currentView = tab.dataset.view;
    document.querySelectorAll('.view').forEach(v =>
      v.classList.toggle('active', v.id === 'view-' + currentView));
    if (currentView === 'log' && !logStarted) startLog();
    if (currentView === 'users') loadUsers();
    if (currentView === 'audit') loadAudit();
    if (currentView === 'manage') loadManage();
    if (currentView === 'console') loadConsole();
    if (currentView === 'world') loadWorld();
    if (currentView === 'map') initMap();
    if (currentView === 'mods') loadMods();
  });
});

$('#logout').addEventListener('click', async () => {
  await fetch('/api/v1/logout', {method: 'POST'});
  location.href = '/login';
});

/* ---------- dashboard ---------- */
async function refresh() {
  if (currentView !== 'dash') return;
  let snap;
  try {
    const r = await fetch('/api/v1/snapshot', {cache: 'no-store'});
    if (r.status === 401) { location.href = '/login'; return; }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    snap = await r.json();
  } catch (e) {
    $('#health-dot').className = 'dot down';
    return;
  }

  const sup = snap.supervisor || {};
  const ag = snap.agent || {};
  const world = ag.world || {};
  const probe = snap.probe || {};
  const log = snap.log || {};

  const dot = $('#health-dot');
  if (sup.reachable && ag.reachable) dot.className = 'dot up';
  else if (sup.reachable) dot.className = 'dot warn';
  else dot.className = 'dot down';

  $('#world-name').textContent = world.name || '—';

  const code = log.join_code;
  $('#joincode').textContent = code || '—';
  const ageS = ageOf(log.join_code_seen_at);
  $('#joincode-age').textContent = ageS != null ? `set ${humanDur(ageS)} ago` : '';
  $('#joincode-hint').textContent = code
    ? 'Changes when the server restarts. Share this code with players.'
    : 'Not available — the log filter strips the registration line and the host probe has not run yet.';

  $('#s-state').textContent = (sup.state || '—').toLowerCase();
  $('#s-uptime').textContent = sup.start
    ? humanDur(Date.now() / 1000 - sup.start)
    : (ag.uptime_seconds != null ? humanDur(ag.uptime_seconds) : '—');
  $('#s-version').textContent = ag.game_version || '—';
  const mem = probe.memory || {};
  $('#s-ram').textContent = mem.current
    ? humanBytes(mem.current) + (mem.limit ? ` / ${humanBytes(mem.limit)}` : '')
    : '—';

  $('#w-day').textContent = world.day != null ? `Day ${world.day}` : '—';
  $('#w-time').textContent = world.day_fraction != null
    ? clockOf(world.day_fraction) + (world.is_night ? ' (night)' : '') : '—';
  $('#w-weather').textContent = world.weather || '—';
  const saveAge = ageOf(log.last_save_ts);
  $('#w-save').textContent = saveAge != null ? `${humanDur(saveAge)} ago` : '—';

  const players = ag.players || [];
  const box = $('#players');
  box.textContent = '';
  $('#p-count').textContent = ag.reachable ? `${players.length}` : '(agent down)';
  if (!players.length) {
    box.appendChild(el('div', 'empty', ag.reachable ? 'Nobody online.' : 'Player list unavailable.'));
  } else {
    players.forEach(p => {
      const row = el('div', 'player');
      row.appendChild(el('span', 'pdot'));
      row.appendChild(el('span', 'pname', p.name || '?'));
      const pos = p.position || p.pos;
      if (pos) {
        const x = Math.round(pos.x ?? pos[0] ?? 0);
        const z = Math.round(pos.z ?? pos[2] ?? 0);
        row.appendChild(el('span', 'pos', `${x}, ${z}`));
      }
      box.appendChild(row);
    });
  }

  const b = snap.backups || {};
  const list = $('#backups');
  list.textContent = '';
  $('#b-count').textContent = b.total ? `${b.total} · ${humanBytes(b.total_size)}` : '';
  const items = (b.items || []).slice(0, 8);
  if (!items.length) {
    list.appendChild(el('div', 'empty', 'No backups yet.'));
  } else {
    items.forEach(it => {
      const row = el('div', 'row');
      row.appendChild(el('span', 'name', it.name));
      row.appendChild(el('span', 'meta',
        `${humanBytes(it.size)} · ${humanDur(Date.now() / 1000 - it.mtime)} ago`));
      list.appendChild(row);
    });
  }

  const mods = log.mods_loaded || [];
  const mbox = $('#mods');
  mbox.textContent = '';
  if (!mods.length) mbox.appendChild(el('div', 'empty', 'No plugins reported.'));
  else mods.forEach(m => mbox.appendChild(el('span', 'chip', m)));

  const errs = (snap.errors || []).concat((log.errors || []).slice(-6));
  const card = $('#err-card'), ebox = $('#errors');
  ebox.textContent = '';
  if (errs.length) {
    card.hidden = false;
    errs.slice(0, 8).forEach(e => ebox.appendChild(el('div', 'e', e)));
  } else {
    card.hidden = true;
  }
}

/* ---------- log ---------- */
let logSource = null;
let follow = true;

const FILTERS = () => {
  const on = {};
  document.querySelectorAll('[data-filter]').forEach(cb => { on[cb.dataset.filter] = cb.checked; });
  return on;
};

function classify(line) {
  const l = line.toLowerCase();
  if (l.includes('player joined')) return 'join';
  if (l.includes('connection lost')) return 'leave';
  if (l.includes('world save') || l.includes('saving')) return 'save';
  if (l.includes('bepinex') || l.includes('loading [')) return 'mod';
  if (l.includes('error') || l.includes('exception') || l.includes('fatal')) return 'error';
  return 'dim';
}

function appendLines(text) {
  const pre = $('#log');
  const on = FILTERS();
  const showAll = on.raw;
  const frag = document.createDocumentFragment();
  text.split('\n').forEach(line => {
    if (!line.trim()) return;
    const kind = classify(line);
    if (!showAll && !on[kind]) return;
    if (!showAll && kind === 'dim') return;
    frag.appendChild(el('span', 'l ' + kind, line));
  });
  if (!frag.childNodes.length) return;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;
  pre.appendChild(frag);
  while (pre.childNodes.length > 4000) pre.removeChild(pre.firstChild);
  if (follow && atBottom) pre.scrollTop = pre.scrollHeight;
}

function startLog() {
  logStarted = true;
  const pre = $('#log');
  pre.textContent = '';
  if (logSource) logSource.close();
  logSource = new EventSource('/api/v1/log/stream');
  logSource.onmessage = ev => {
    try {
      const d = JSON.parse(ev.data);
      if (d.text) appendLines(d.text);
    } catch (_) { /* ignore malformed frame */ }
  };
  logSource.onerror = () => appendLines('--- log stream interrupted, retrying ---');
}

$('#follow').addEventListener('change', ev => {
  follow = ev.target.checked;
  if (follow) { const p = $('#log'); p.scrollTop = p.scrollHeight; }
});
$('#clear-log').addEventListener('click', () => { $('#log').textContent = ''; });
document.querySelectorAll('[data-filter]').forEach(cb => {
  cb.addEventListener('change', () => { if (logStarted) startLog(); });
});

/* ---------- users ---------- */
async function loadUsers() {
  try {
    const r = await fetch('/api/v1/users', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    renderUsers(d.users || []);
    renderMatrix(d.roles || []);
  } catch (_) { /* ignore */ }
}

function renderUsers(users) {
  const box = $('#user-list');
  box.textContent = '';
  $('#u-count').textContent = `${users.length}`;
  if (!users.length) {
    box.appendChild(el('div', 'empty', 'No users.'));
    return;
  }
  users.forEach(u => {
    const row = el('div', 'userrow');

    const left = el('div', 'uleft');
    left.appendChild(el('span', 'uname', u.username));
    if (u.username === me) left.appendChild(el('span', 'tag', 'you'));
    if (u.disabled) left.appendChild(el('span', 'tag bad', 'disabled'));
    const badge = el('span', 'rolebadge role-' + u.role, u.role);
    left.appendChild(badge);
    const last = u.last_login_at
      ? `last login ${whenOf(u.last_login_at)}`
      : 'never signed in';
    left.appendChild(el('span', 'umeta', last));
    row.appendChild(left);

    const right = el('div', 'uright');

    const roleSel = el('select', 'rolesel');
    ['user', 'mod', 'admin'].forEach(r => {
      const o = el('option', undefined, r);
      o.value = r;
      if (r === u.role) o.selected = true;
      roleSel.appendChild(o);
    });
    roleSel.addEventListener('change', async () => {
      try {
        await post('/api/v1/users/update', {username: u.username, role: roleSel.value});
        loadUsers();
      } catch (e) { alert('Failed: ' + e.message); loadUsers(); }
    });
    right.appendChild(roleSel);

    const resetBtn = el('button', 'ghost small', 'Reset password');
    resetBtn.addEventListener('click', async () => {
      const pw = prompt(`New password for ${u.username} (min 10 chars):`);
      if (!pw) return;
      try {
        await post('/api/v1/users/update', {username: u.username, password: pw});
        alert('Password changed. Their sessions were signed out.');
        loadUsers();
      } catch (e) { alert('Failed: ' + e.message); }
    });
    right.appendChild(resetBtn);

    const toggleBtn = el('button', 'ghost small', u.disabled ? 'Enable' : 'Disable');
    toggleBtn.addEventListener('click', async () => {
      try {
        await post('/api/v1/users/update', {username: u.username, disabled: !u.disabled});
        loadUsers();
      } catch (e) { alert('Failed: ' + e.message); }
    });
    right.appendChild(toggleBtn);

    if (u.username !== me) {
      const delBtn = el('button', 'ghost small danger', 'Delete');
      delBtn.addEventListener('click', async () => {
        if (!confirm(`Delete user "${u.username}"? This cannot be undone.`)) return;
        try {
          await post('/api/v1/users/delete', {username: u.username});
          loadUsers();
        } catch (e) { alert('Failed: ' + e.message); }
      });
      right.appendChild(delBtn);
    }

    row.appendChild(right);
    box.appendChild(row);
  });
}

function renderMatrix(roleList) {
  const box = $('#role-matrix');
  box.textContent = '';
  roleList.forEach(r => {
    const col = el('div', 'mcol');
    col.appendChild(el('div', 'mhead role-' + r.role, r.label));
    r.capabilities.forEach(c => col.appendChild(el('div', 'mcap', c)));
    box.appendChild(col);
  });
}

$('#user-form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const errBox = $('#user-form-err');
  errBox.textContent = '';
  try {
    await post('/api/v1/users', {
      username: $('#nu').value.trim(),
      role: $('#nr').value,
      password: $('#np').value
    });
    $('#nu').value = ''; $('#np').value = '';
    loadUsers();
  } catch (e) {
    errBox.textContent = e.message;
  }
});

/* ---------- audit ---------- */
async function loadAudit() {
  const params = new URLSearchParams({limit: '300'});
  const actor = $('#af-actor').value.trim();
  const action = $('#af-action').value.trim();
  if (actor) params.set('actor', actor);
  if (action) params.set('action', action);

  try {
    const r = await fetch('/api/v1/audit?' + params, {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    renderAudit(d.entries || []);
  } catch (_) { /* ignore */ }
}

function renderAudit(entries) {
  const box = $('#audit-list');
  box.textContent = '';
  $('#audit-count').textContent = `${entries.length} entries`;
  if (!entries.length) {
    box.appendChild(el('div', 'empty', 'Nothing recorded yet.'));
    return;
  }
  entries.forEach(e => {
    const row = el('div', 'arow' + (e.ok ? '' : ' failed'));
    row.appendChild(el('span', 'ats', whenOf(e.ts)));
    row.appendChild(el('span', 'aactor', e.actor));
    row.appendChild(el('span', 'arole role-' + e.role, e.role));
    row.appendChild(el('span', 'aaction', e.action));
    row.appendChild(el('span', 'atarget', e.target || ''));
    const det = e.detail ? (e.detail.length > 90 ? e.detail.slice(0, 90) + '…' : e.detail) : '';
    row.appendChild(el('span', 'adetail', det));
    row.appendChild(el('span', 'aip', e.ip || ''));
    box.appendChild(row);
  });
}

$('#af-apply').addEventListener('click', loadAudit);

/* ---------- account modal ---------- */
const dlg = $('#account-dialog');
$('#account-btn').addEventListener('click', () => {
  $('#acc-who').textContent = `${me} · ${myRole}`;
  $('#acc-err').textContent = '';
  $('#cp').value = ''; $('#npw').value = '';
  dlg.showModal();
});

$('#acc-save').addEventListener('click', async () => {
  const err = $('#acc-err');
  err.textContent = '';
  try {
    await post('/api/v1/password', {current: $('#cp').value, new: $('#npw').value});
    err.textContent = 'Password changed.';
    err.style.color = 'var(--good)';
    $('#cp').value = ''; $('#npw').value = '';
  } catch (e) {
    err.style.color = 'var(--bad)';
    err.textContent = e.message;
  }
});

/* ---------- manage ---------- */
let currentList = 'admin';
let lastSnapshot = null;

const LIST_HINTS = {
  admin: 'Admins can use the in-game console and devcommands. Changes apply to the next join.',
  banned: 'Banned players cannot connect. Valheim reloads this list live.',
  permitted: 'Adding any entry turns on the vanilla allow-list: only these players may join.',
};

async function loadManage() {
  await Promise.all([loadList(currentList), loadWorlds(), refreshControlState()]);
}

async function refreshControlState() {
  try {
    const r = await fetch('/api/v1/snapshot', {cache: 'no-store'});
    if (!r.ok) return;
    lastSnapshot = await r.json();
    const st = (lastSnapshot.supervisor?.state || '').toUpperCase();
    const running = st === 'RUNNING';
    // Disable the actions that cannot apply in the current state.
    if ($('#btn-restart')) $('#btn-restart').disabled = !running;
    if ($('#btn-stop')) $('#btn-stop').disabled = !running;
    if ($('#btn-start')) $('#btn-start').disabled = running;
    if ($('#btn-save')) $('#btn-save').disabled = !running;
    $('#ctl-hint').textContent = running
      ? 'Server is running. Restart saves the world and brings it back in about a minute.'
      : 'Server is stopped. Start it to accept joins.';
  } catch (_) { /* ignore */ }
}

async function serverAction(action) {
  const msg = $('#ctl-msg');
  msg.textContent = '';
  msg.style.color = 'var(--dim)';

  let body = {};
  if (action === 'stop') {
    const players = lastSnapshot?.agent?.players?.length || 0;
    const warn = players
      ? `\n\n${players} player(s) are online and will be disconnected.`
      : '';
    if (!confirm(`Stop the server?${warn}\n\nThe world is saved first.`)) return;
    body.force = players > 0;
  }
  if (action === 'restart' && !confirm('Restart the server? Players will be disconnected.')) return;

  msg.textContent = `${action}…`;
  try {
    const d = await post('/api/v1/server/' + action, body);
    msg.style.color = 'var(--good)';
    msg.textContent = d.message || 'ok';
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
  setTimeout(refreshControlState, 1500);
}

document.querySelectorAll('#server-controls [data-action]').forEach(btn => {
  btn.addEventListener('click', () => serverAction(btn.dataset.action));
});

$('#backup-now').addEventListener('click', async () => {
  try {
    const d = await post('/api/v1/backups/create', {});
    alert(d.message || 'backup requested');
    refresh();
  } catch (e) { alert('Failed: ' + e.message); }
});

/* ---------- player lists ---------- */
document.querySelectorAll('.ltab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.ltab').forEach(t => t.classList.toggle('active', t === tab));
    currentList = tab.dataset.list;
    loadList(currentList);
  });
});

async function loadList(kind) {
  const box = $('#list-entries');
  const msg = $('#list-msg');
  const warn = $('#list-warning');
  msg.textContent = '';
  try {
    const r = await fetch('/api/v1/lists/' + kind, {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();

    if (d.warning) {
      warn.hidden = false;
      warn.textContent = d.warning;
    } else {
      warn.hidden = true;
    }

    const editable = d.can_edit && can('lists.edit');
    $('#list-input').disabled = !editable;
    $('#list-add').disabled = !editable;
    $('#list-hint').textContent = LIST_HINTS[kind] +
      (editable ? '' : ' You do not have permission to edit this list.');

    box.textContent = '';
    const ids = d.ids || [];
    if (!ids.length) {
      box.appendChild(el('div', 'empty', 'Empty.'));
      return;
    }
    ids.forEach(id => {
      const row = el('div', 'row');
      row.appendChild(el('span', 'name', id));
      if (editable) {
        const rm = el('button', 'ghost small danger', 'remove');
        rm.addEventListener('click', () => saveList(kind, ids.filter(x => x !== id)));
        row.appendChild(rm);
      }
      box.appendChild(row);
    });
  } catch (_) { /* ignore */ }
}

async function saveList(kind, ids) {
  const msg = $('#list-msg');
  msg.textContent = '';
  try {
    const d = await post('/api/v1/lists/' + kind, {ids});
    msg.style.color = 'var(--good)';
    msg.textContent = d.message || 'saved';
    if (d.warning) {
      $('#list-warning').hidden = false;
      $('#list-warning').textContent = d.warning;
    }
    loadList(kind);
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
}

$('#list-add').addEventListener('click', async () => {
  const input = $('#list-input');
  const value = input.value.trim();
  const msg = $('#list-msg');
  if (!/^\d{17}$/.test(value)) {
    msg.style.color = 'var(--bad)';
    msg.textContent = 'SteamID64 must be exactly 17 digits.';
    return;
  }
  const r = await fetch('/api/v1/lists/' + currentList, {cache: 'no-store'});
  const d = await r.json();
  const ids = d.ids || [];
  if (ids.includes(value)) {
    msg.style.color = 'var(--dim)';
    msg.textContent = 'Already in the list.';
    return;
  }
  input.value = '';
  saveList(currentList, ids.concat([value]));
});

$('#list-input').addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); $('#list-add').click(); }
});

/* ---------- worlds ---------- */
async function loadWorlds() {
  const box = $('#world-list');
  box.textContent = '';
  try {
    const r = await fetch('/api/v1/snapshot', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const active = d.world?.active;
    const items = d.world?.items || [];
    $('#wcount').textContent = `${items.length}`;
    if (!items.length) {
      box.appendChild(el('div', 'empty', 'No worlds found.'));
      return;
    }
    items.slice(0, 20).forEach(w => {
      const row = el('div', 'row');
      const left = el('div', 'wleft');
      left.appendChild(el('span', 'name', w.name));
      if (w.name === active) left.appendChild(el('span', 'tag good', 'active'));
      if (w.is_backup) left.appendChild(el('span', 'tag', 'backup'));
      row.appendChild(left);

      const meta = el('span', 'meta',
        `${humanBytes(w.size)} · gen ${w.generation ?? '?'}`);
      row.appendChild(meta);

      if (!w.is_backup && can('world.regenerate')) {
        const right = el('div', 'uright');
        if (w.name === active) {
          const rg = el('button', 'ghost small', 'regenerate');
          rg.addEventListener('click', () => worldAction('regenerate', w.name));
          right.appendChild(rg);
        } else {
          const del = el('button', 'ghost small danger', 'delete');
          del.addEventListener('click', () => worldAction('delete', w.name));
          right.appendChild(del);
        }
        row.appendChild(right);
      }
      box.appendChild(row);
    });
  } catch (_) { /* ignore */ }
}

async function worldAction(action, name) {
  const what = action === 'delete'
    ? `Delete world "${name}"?\n\nIt is archived as a zip in backups first, so this is recoverable.\nThe server must be stopped.`
    : `Regenerate "${name}"?\n\nThe current world is archived as a zip, then Valheim creates a fresh world with a NEW RANDOM SEED on the next start.\nBases, builds and progress in the new world are gone.\nThe server must be stopped.`;
  if (!confirm(what)) return;
  try {
    const d = await post('/api/v1/worlds/' + action, {name});
    alert(d.message || 'ok');
    loadWorlds();
  } catch (e) { alert('Failed: ' + e.message); }
}

/* ---------- console ---------- */
let cmdCatalog = [];
let chatCursor = 0;

async function loadConsole() {
  await Promise.all([loadPlayers(), loadCatalog(), loadChat()]);
}

async function loadPlayers() {
  const box = $('#cp-list');
  box.textContent = '';
  try {
    const r = await fetch('/api/v1/players', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const list = d.players || [];
    $('#cp-count').textContent = d.agent_available
      ? `${list.length}`
      : '(agent down)';

    if (!list.length) {
      box.appendChild(el('div', 'empty',
        d.agent_available ? 'Nobody online.' : 'Player list unavailable.'));
      return;
    }
    list.forEach(p => {
      const row = el('div', 'player');
      row.appendChild(el('span', 'pdot'));
      row.appendChild(el('span', 'pname', p.name || '?'));
      const pos = p.position || p.pos;
      if (pos) {
        const x = Math.round(pos.x ?? pos[0] ?? 0);
        const z = Math.round(pos.z ?? pos[2] ?? 0);
        row.appendChild(el('span', 'pos', `${x}, ${z}`));
      }
      if (d.can_kick && p.name) {
        const k = el('button', 'ghost small', 'kick');
        k.addEventListener('click', () => {
          if (confirm(`Kick ${p.name}?`)) runCommand('kick', {player: p.name});
        });
        row.appendChild(k);
      }
      box.appendChild(row);
    });
  } catch (_) { /* ignore */ }
}

async function loadCatalog() {
  try {
    const r = await fetch('/api/v1/commands', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    cmdCatalog = (d.commands || []).filter(c => c.allowed);
    const sel = $('#cmd-select');
    sel.textContent = '';
    if (!cmdCatalog.length) {
      sel.appendChild(el('option', undefined, 'no commands available'));
      $('#cmd-run').disabled = true;
      return;
    }
    cmdCatalog.forEach(c => {
      const o = el('option', undefined,
        c.name + (c.dangerous ? '  ⚠' : '') + ' — ' + c.summary);
      o.value = c.name;
      sel.appendChild(o);
    });
    renderArgs();
  } catch (_) { /* ignore */ }
}

function renderArgs() {
  const box = $('#cmd-args');
  box.textContent = '';
  const sel = $('#cmd-select').value;
  const spec = cmdCatalog.find(c => c.name === sel);
  if (!spec) return;
  (spec.args || []).forEach(arg => {
    const wrap = el('div', 'f');
    wrap.appendChild(el('label', undefined, arg));
    const inp = el('input');
    inp.type = 'text';
    inp.dataset.arg = arg;
    inp.placeholder = arg;
    wrap.appendChild(inp);
    box.appendChild(wrap);
  });
}

$('#cmd-select').addEventListener('change', renderArgs);

async function runCommand(name, args, silent) {
  const msg = $('#cmd-msg');
  const out = $('#cmd-out');
  msg.textContent = '';
  try {
    const d = await post('/api/v1/commands/run', {name, args: args || {}});
    if (!silent) {
      msg.style.color = d.ok ? 'var(--good)' : 'var(--bad)';
      msg.textContent = d.ok ? 'ok' : 'command reported failure';
      out.hidden = false;
      out.textContent = d.output || '(no output)';
    }
    loadPlayers();
    return d;
  } catch (e) {
    if (!silent) {
      msg.style.color = 'var(--bad)';
      msg.textContent = e.message;
    }
    return null;
  }
}

$('#cmd-run').addEventListener('click', async () => {
  const name = $('#cmd-select').value;
  const spec = cmdCatalog.find(c => c.name === name);
  if (!spec) return;

  const args = {};
  document.querySelectorAll('#cmd-args [data-arg]').forEach(inp => {
    args[inp.dataset.arg] = inp.value;
  });

  if (spec.dangerous) {
    const preview = Object.entries(args).filter(([, v]) => v)
      .map(([k, v]) => `${k}=${v}`).join(' ');
    if (!confirm(`Run ${name}${preview ? ' ' + preview : ''}?\n\nThis changes server state.`)) return;
  }
  await runCommand(name, args);
});

/* quick message row */
async function sendMessage(name) {
  const input = $('#qc-message');
  const text = input.value.trim();
  const msg = $('#qc-msg');
  if (!text) return;
  try {
    const d = await post('/api/v1/commands/run', {name, args: {message: text}});
    msg.style.color = 'var(--good)';
    msg.textContent = d.output || 'sent';
    input.value = '';
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
}

$('#qc-broadcast').addEventListener('click', () => sendMessage('broadcast'));
$('#qc-say').addEventListener('click', () => sendMessage('say'));
$('#qc-save').addEventListener('click', async () => {
  const msg = $('#qc-msg');
  try {
    const d = await post('/api/v1/commands/run', {name: 'save', args: {}});
    msg.style.color = 'var(--good)';
    msg.textContent = d.output || 'saved';
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
});
$('#qc-message').addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); $('#qc-broadcast').click(); }
});

/* free-form */
$('#ff-run').addEventListener('click', async () => {
  const input = $('#ff-input');
  const raw = input.value.trim();
  const msg = $('#ff-msg');
  if (!raw) return;
  msg.textContent = '';
  try {
    const d = await post('/api/v1/commands/freeform', {command: raw});
    msg.style.color = 'var(--good)';
    msg.textContent = d.output || '(no output)';
    msg.style.whiteSpace = 'pre-wrap';
    input.value = '';
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
});
$('#ff-input').addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); $('#ff-run').click(); }
});

/* chat */
async function loadChat() {
  const box = $('#chat-list');
  try {
    const r = await fetch('/api/v1/chat?since=' + chatCursor, {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const msgs = d.messages || [];
    if (typeof d.next === 'number') chatCursor = d.next;
    if (!msgs.length && !box.childNodes.length) {
      box.appendChild(el('div', 'empty', 'No chat yet.'));
      return;
    }
    if (msgs.length) {
      const empty = box.querySelector('.empty');
      if (empty) empty.remove();
    }
    msgs.forEach(m => {
      const row = el('div', 'chatmsg');
      row.appendChild(el('span', 'cwho', m.name || m.sender || '?'));
      row.appendChild(el('span', 'ctext', m.text || m.message || ''));
      box.appendChild(row);
    });
    $('#chat-count').textContent = box.querySelectorAll('.chatmsg').length || '';
    while (box.childNodes.length > 300) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  } catch (_) { /* ignore */ }
}

/* ---------- live world ---------- */
let worldTimer = null;

const BIOME_LABEL = {
  Meadows: 'Meadows', BlackForest: 'Black Forest', Swamp: 'Swamp',
  Mountain: 'Mountain', Plains: 'Plains', Mistlands: 'Mistlands',
  AshLands: 'Ashlands', DeepNorth: 'Deep North', Ocean: 'Ocean',
  Edge: 'Edge'
};

function prettyKey(k) {
  return k.replace(/^defeated_/, 'defeated ').replace(/_/g, ' ');
}

async function loadWorld() {
  await Promise.all([loadWorldState(), loadEvents()]);
  if (worldTimer) clearInterval(worldTimer);
  worldTimer = setInterval(() => {
    if (currentView === 'world') { loadWorldState(); loadEvents(); }
  }, 5000);
}

async function loadWorldState() {
  try {
    const r = await fetch('/api/v1/world', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const w = d.world || {};

    $('#lw-day').textContent = w.day != null ? `Day ${w.day}` : '—';
    $('#lw-time').textContent = w.day_fraction != null
      ? clockOf(w.day_fraction) + (w.is_night ? ' (night)' : '') : '—';
    $('#lw-weather').textContent = w.weather || '—';
    $('#lw-event').textContent = w.event
      ? `${w.event.name} (${Math.round(w.event.remaining_seconds || 0)}s left)`
      : 'none';

    $('#lw-events').textContent = d.event_count ?? '—';
    $('#lw-deaths').textContent = d.death_count ?? '—';
    const p = d.poller || {};
    $('#lw-poller').textContent = p.running
      ? (p.last_error ? 'error' : 'ok')
      : 'stopped';

    /* players */
    const box = $('#lw-players');
    box.textContent = '';
    const players = d.players || [];
    $('#lw-pcount').textContent = `${players.length}`;
    $('#lw-poshint').textContent = d.can_see_positions
      ? ''
      : 'Positions of players hidden on the map are withheld for your role.';

    if (!players.length) {
      box.appendChild(el('div', 'empty', 'Nobody online.'));
    } else {
      players.forEach(pl => {
        const row = el('div', 'player');
        row.appendChild(el('span', 'pdot'));
        row.appendChild(el('span', 'pname', pl.name || '?'));
        if (pl.biome) {
          row.appendChild(el('span', 'pos', BIOME_LABEL[pl.biome] || pl.biome));
        }
        const pos = pl.position;
        if (pos) {
          const x = Math.round(pos.x ?? pos[0] ?? 0);
          const z = Math.round(pos.z ?? pos[2] ?? 0);
          row.appendChild(el('span', 'pos', `${x}, ${z}`));
        } else if (pl.hidden) {
          row.appendChild(el('span', 'pos', 'position hidden'));
        }
        box.appendChild(row);
      });
    }

    /* progression keys, minus modifier noise */
    const keys = d.global_keys || [];
    const kbox = $('#lw-keys');
    kbox.textContent = '';
    const progression = keys.filter(k => !/^[a-z]+[A-Z]/.test(k));
    if (!progression.length) {
      kbox.appendChild(el('div', 'empty', 'No progression keys yet.'));
    } else {
      progression.forEach(k => kbox.appendChild(el('span', 'chip', prettyKey(k))));
    }
  } catch (_) { /* ignore */ }
}

async function loadDeaths() {
  try {
    const r = await fetch('/api/v1/deaths', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const box = $('#lw-deathboard');
    box.textContent = '';
    const rows = d.summary || [];
    if (!rows.length) {
      box.appendChild(el('div', 'empty', 'No deaths recorded yet.'));
      return;
    }
    rows.slice(0, 10).forEach((row, i) => {
      const line = el('div', 'row');
      const left = el('div', 'wleft');
      left.appendChild(el('span', 'rank', `${i + 1}.`));
      left.appendChild(el('span', 'name', row.character || '?'));
      line.appendChild(left);
      line.appendChild(el('span', 'meta',
        `${row.deaths} · ${humanDur(Date.now() / 1000 - row.last_at)} ago`));
      box.appendChild(line);
    });
  } catch (_) { /* ignore */ }
}

async function loadEvents() {
  loadDeaths();
  try {
    const r = await fetch('/api/v1/events?limit=120', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const showJoins = $('#ev-joins').checked;
    const showDeaths = $('#ev-deaths').checked;
    const showCmds = $('#ev-cmds').checked;

    const box = $('#lw-events-list');
    box.textContent = '';
    const rows = (d.events || []).filter(e => {
      if (e.kind === 'player.join' || e.kind === 'player.leave') return showJoins;
      if (e.kind === 'player.death') return showDeaths;
      if (e.kind === 'command') return showCmds;
      return showJoins;
    });

    if (!rows.length) {
      box.appendChild(el('div', 'empty', 'Nothing recorded yet.'));
      return;
    }
    rows.slice(0, 60).forEach(e => {
      const row = el('div', 'eventrow');
      row.appendChild(el('span', 'ekind ' + e.kind.replace('.', '-'),
        e.kind.replace('player.', '')));
      row.appendChild(el('span', 'eactor', e.actor || e.character || '—'));
      if (e.day != null) row.appendChild(el('span', 'eday', `day ${e.day}`));
      if (e.biome) row.appendChild(el('span', 'ebiome', BIOME_LABEL[e.biome] || e.biome));
      row.appendChild(el('span', 'ewhen', humanDur(Date.now() / 1000 - e.at) + ' ago'));
      box.appendChild(row);
    });
  } catch (_) { /* ignore */ }
}

['#ev-joins', '#ev-deaths', '#ev-cmds'].forEach(sel => {
  const node = $(sel);
  if (node) node.addEventListener('change', loadEvents);
});

/* ---------- mods ---------- */
let modInstalled = [];

async function loadMods() {
  await Promise.all([loadInstalled(), loadSnapshots(), loadConfigList(), loadGameplay()]);
}

function tierBadge(tier) {
  const cls = {
    'server-only': 'good',
    'declared-safe': 'good',
    'unknown': '',
    'client-required': 'bad',
  }[tier] ?? '';
  const label = {
    'server-only': 'server-only',
    'declared-safe': 'console-safe*',
    'unknown': 'unclassified',
    'client-required': 'needs client',
  }[tier] ?? tier;
  return el('span', 'tag ' + cls, label);
}

async function loadInstalled() {
  const box = $('#mod-installed');
  box.textContent = '';
  try {
    const r = await fetch('/api/v1/mods', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    modInstalled = d.installed || [];

    const banner = $('#cp-banner');
    if (d.crossplay) {
      banner.hidden = false;
      banner.innerHTML = '';
      const h = el('div', 'warnbox');
      h.textContent = 'Crossplay is ON. Server-only mods are safe. A mod needing a ' +
        'client install will block console players — the panel warns, it does not block.';
      banner.appendChild(h);
    } else {
      banner.hidden = true;
    }

    $('#mod-count').textContent = `${modInstalled.length}`;
    const unmanaged = d.unmanaged || [];
    $('#mod-unmanaged').textContent = unmanaged.length
      ? `Not managed by the panel: ${unmanaged.join(', ')}`
      : '';

    if (!modInstalled.length) {
      box.appendChild(el('div', 'empty', 'No panel-managed mods yet.'));
      return;
    }

    modInstalled.forEach(m => {
      const row = el('div', 'modrow');
      const left = el('div', 'mleft');
      left.appendChild(el('span', 'mname', m.full_name));
      left.appendChild(el('span', 'mver', m.version));
      left.appendChild(tierBadge(m.tier));
      if (m.pinned) left.appendChild(el('span', 'tag', 'pinned'));
      if (m.disabled) left.appendChild(el('span', 'tag bad', 'disabled'));
      if (m.custom_url) left.appendChild(el('span', 'tag', 'custom'));
      if ((m.dependencies || []).length) {
        left.appendChild(el('span', 'mdeps', `+${m.dependencies.length} deps`));
      }
      row.appendChild(left);

      const right = el('div', 'uright');
      if (can('mod.install')) {
        const t = el('button', 'ghost small', m.disabled ? 'enable' : 'disable');
        t.addEventListener('click', () => modAction(m.disabled ? 'enable' : 'disable',
          {name: m.full_name}));
        right.appendChild(t);

        const pin = el('button', 'ghost small', m.pinned ? 'unpin' : 'pin');
        pin.addEventListener('click', () => modAction('pin',
          {name: m.full_name, pinned: !m.pinned}));
        right.appendChild(pin);
      }
      if (can('mod.quarantine')) {
        const q = el('button', 'ghost small danger', 'quarantine');
        q.addEventListener('click', () => {
          const reason = prompt(`Quarantine ${m.full_name}?\n\nReason (recorded in the manifest):`);
          if (reason === null) return;
          modAction('quarantine', {name: m.full_name, reason: reason || 'reported broken'});
        });
        right.appendChild(q);
      }
      if (can('mod.uninstall')) {
        const u = el('button', 'ghost small danger', 'uninstall');
        u.addEventListener('click', () => {
          if (!confirm(`Uninstall ${m.full_name}?\n\nA snapshot is taken first.`)) return;
          modAction('uninstall', {name: m.full_name});
        });
        right.appendChild(u);
      }
      row.appendChild(right);
      box.appendChild(row);
    });

    /* quarantined */
    const qs = d.quarantined || [];
    const qcard = $('#mod-quarantine-card');
    const qbox = $('#mod-quarantine');
    qbox.textContent = '';
    if (qs.length) {
      qcard.hidden = false;
      qs.forEach(q => {
        const row = el('div', 'modrow');
        const left = el('div', 'mleft');
        left.appendChild(el('span', 'mname', q.full_name));
        left.appendChild(tierBadge(q.tier));
        const reason = q.quarantine?.reason;
        if (reason) left.appendChild(el('span', 'mdeps', reason));
        row.appendChild(left);
        if (can('mod.install')) {
          const en = el('button', 'ghost small', 're-enable anyway');
          en.addEventListener('click', () => modAction('enable', {name: q.full_name}));
          row.appendChild(en);
        }
        qbox.appendChild(row);
      });
    } else {
      qcard.hidden = true;
    }
  } catch (_) { /* ignore */ }
}

async function modAction(action, body) {
  try {
    const d = await post('/api/v1/mods/' + action, body);
    if (d.needs_restart) {
      alert((d.message || 'done') + '\n\nRestart the server for this to take effect.');
    }
    loadInstalled();
    loadSnapshots();
    return d;
  } catch (e) {
    alert('Failed: ' + e.message);
    return null;
  }
}

/* search */
async function searchMods() {
  const box = $('#mod-results');
  const msg = $('#mod-search-msg');
  msg.textContent = '';
  box.textContent = '';
  const params = new URLSearchParams({
    q: $('#mod-q').value.trim(),
    store: $('#mod-store').value,
    limit: '40',
  });
  if ($('#mod-serveronly').checked) params.set('server_only', 'true');

  try {
    const r = await fetch('/api/v1/mods/search?' + params, {cache: 'no-store'});
    const d = await r.json();
    if (!r.ok) { msg.textContent = d.error || ('HTTP ' + r.status); return; }
    if (d.warming) {
      msg.style.color = 'var(--dim)';
      msg.textContent = d.message || 'building index…';
      setTimeout(searchMods, 4000);
      return;
    }
    const results = d.results || [];
    if (!results.length) {
      box.appendChild(el('div', 'empty', 'No matches.'));
      return;
    }
    results.forEach(pkg => {
      const card = el('div', 'modcard');
      const head = el('div', 'mheadrow');
      head.appendChild(el('span', 'mname', pkg.name));
      head.appendChild(el('span', 'mowner', pkg.owner));
      head.appendChild(el('span', 'mver', pkg.version));
      head.appendChild(el('span', 'tag', pkg.store));
      head.appendChild(tierBadge(pkg.verdict?.tier));
      card.appendChild(head);

      card.appendChild(el('p', 'mdesc', (pkg.description || '').slice(0, 180)));

      const pol = pkg.policy || {};
      if (pol.warn) {
        const w = el('div', 'modwarn ' + (pol.level === 'danger' ? 'danger' : ''));
        w.textContent = pol.reason;
        card.appendChild(w);
      }
      if (pkg.verdict?.evidence?.length) {
        card.appendChild(el('div', 'mevidence', pkg.verdict.evidence.join(' · ')));
      }

      const actions = el('div', 'mactions');
      actions.appendChild(el('span', 'mstat',
        `${pkg.downloads.toLocaleString()} downloads`));
      if (can('mod.install')) {
        const btn = el('button', 'btn small', 'Install');
        btn.addEventListener('click', () => installMod(pkg, btn));
        actions.appendChild(btn);
      }
      card.appendChild(actions);
      box.appendChild(card);
    });
  } catch (e) {
    msg.textContent = e.message;
  }
}

async function installMod(pkg, btn) {
  const pol = pkg.policy || {};
  let acknowledge = false;
  if (pol.level === 'danger') {
    acknowledge = confirm(
      `⚠ ${pkg.name} needs a client install.\n\n` +
      `${pol.reason}\n\nInstall anyway? Console players will be unable to join.`);
    if (!acknowledge) return;
  }
  btn.disabled = true;
  btn.textContent = 'installing…';
  try {
    const d = await post('/api/v1/mods/install', {
      name: pkg.full_name, acknowledge
    });
    btn.textContent = 'installed';
    alert(`Installed ${pkg.name}${d.installed?.length > 1 ? ' + dependencies' : ''}.\n\n` +
          `Restart the server to load it.`);
    loadInstalled();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Install';
    alert('Failed: ' + e.message);
  }
}

$('#mod-search').addEventListener('click', searchMods);
$('#mod-q').addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { ev.preventDefault(); searchMods(); }
});

$('#mod-import').addEventListener('click', async () => {
  const msg = $('#mod-import-msg');
  const url = $('#mod-url').value.trim();
  if (!url) return;
  msg.textContent = 'fetching…';
  msg.style.color = 'var(--dim)';
  try {
    const d = await post('/api/v1/mods/import-url',
      {url, name: $('#mod-url-name').value.trim()});
    msg.style.color = 'var(--good)';
    msg.textContent = `imported as ${d.name} — restart to load`;
    $('#mod-url').value = ''; $('#mod-url-name').value = '';
    loadInstalled();
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
});

/* config editor */
async function loadConfigList() {
  try {
    const r = await fetch('/api/v1/mods/config', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const sel = $('#mod-cfg-select');
    sel.textContent = '';
    (d.files || []).forEach(f => {
      const o = el('option', undefined, f.name);
      o.value = f.name;
      sel.appendChild(o);
    });
  } catch (_) { /* ignore */ }
}

$('#mod-cfg-load').addEventListener('click', async () => {
  const name = $('#mod-cfg-select').value;
  const msg = $('#mod-cfg-msg');
  const ta = $('#mod-cfg-text');
  if (!name) return;
  try {
    const r = await fetch('/api/v1/mods/config?name=' + encodeURIComponent(name),
      {cache: 'no-store'});
    const d = await r.json();
    if (!r.ok) { msg.textContent = d.error; return; }
    ta.value = d.content || '';
    ta.hidden = false;
    $('#mod-cfg-save').hidden = !can('mod.install');
    msg.textContent = '';
  } catch (e) { msg.textContent = e.message; }
});

$('#mod-cfg-save').addEventListener('click', async () => {
  const name = $('#mod-cfg-select').value;
  const msg = $('#mod-cfg-msg');
  try {
    await post('/api/v1/mods/config-write', {name, content: $('#mod-cfg-text').value});
    msg.style.color = 'var(--good)';
    msg.textContent = 'saved (a .bak was kept). Most mods need a restart.';
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
});

/* snapshots */
async function loadSnapshots() {
  const box = $('#mod-snapshots');
  if (!box) return;
  box.textContent = '';
  try {
    const r = await fetch('/api/v1/mods/snapshots', {cache: 'no-store'});
    if (!r.ok) return;
    const d = await r.json();
    const items = d.snapshots || [];
    if (!items.length) {
      box.appendChild(el('div', 'empty', 'No snapshots yet.'));
      return;
    }
    items.slice(0, 10).forEach(s => {
      const row = el('div', 'row');
      row.appendChild(el('span', 'name', s.name));
      const meta = el('span', 'meta',
        `${humanBytes(s.size)} · ${humanDur(Date.now() / 1000 - s.mtime)} ago`);
      row.appendChild(meta);
      if (can('mod.install')) {
        const b = el('button', 'ghost small', 'restore');
        b.addEventListener('click', () => {
          if (!confirm(`Restore ${s.name}?\n\nThis replaces the managed plugin set.`)) return;
          modAction('restore-snapshot', {name: s.name});
        });
        row.appendChild(b);
      }
      box.appendChild(row);
    });
  } catch (_) { /* ignore */ }
}

/* ---------- gameplay helpers ---------- */
let gpKnownEvents = [];

async function loadGameplay() {
  const msg = $('#gp-msg');
  const body = $('#gp-body');
  try {
    const r = await fetch('/api/v1/gameplay', {cache: 'no-store'});
    const d = await r.json();
    if (!d.available) {
      $('#gp-hint').textContent = d.error || 'gameplay plugin unavailable';
      body.hidden = true;
      return;
    }
    body.hidden = false;
    gpKnownEvents = d.known_events || [];

    const af = d.autofeed || {};
    $('#gp-af-enabled').checked = !!af.enabled;
    $('#gp-af-debug').checked = !!af.debug;
    $('#gp-af-prefix').value = af.sign_prefix ?? 'feed';
    $('#gp-af-signradius').value = af.sign_radius ?? 2;
    $('#gp-af-range').value = af.range ?? 20;
    $('#gp-af-interval').value = af.interval_seconds ?? 30;

    const raids = d.raids || {};
    $('#gp-raid-enabled').checked = !!raids.enabled;
    renderRaids(raids.disabled || [], raids.unknown_disabled || []);

    const editable = d.can_edit && can('mod.install');
    document.querySelectorAll('#gp-body input').forEach(el => { el.disabled = !editable; });
    $('#gp-save').hidden = !editable;

    const mode = d.reload_mode;
    $('#gp-reload').textContent = mode === 'polled'
      ? 'Last reload used the polling fallback (the file watcher missed the change).'
      : mode === 'watcher'
        ? 'Last reload used the file watcher.'
        : '';
    msg.textContent = '';
  } catch (e) {
    $('#gp-hint').textContent = 'could not load gameplay settings';
    body.hidden = true;
  }
}

function renderRaids(disabled, unknown) {
  const box = $('#gp-raids');
  box.textContent = '';
  if (!gpKnownEvents.length) {
    box.appendChild(el('div', 'empty', 'Event list unavailable (agent offline).'));
    return;
  }
  gpKnownEvents.forEach(name => {
    const id = 'raid-' + name;
    const wrap = el('label', 'raiditem');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.id = id;
    cb.dataset.event = name;
    cb.checked = disabled.includes(name);
    wrap.appendChild(cb);
    wrap.appendChild(el('span', undefined, name.replace(/_/g, ' ')));
    box.appendChild(wrap);
  });
  if (unknown.length) {
    const warn = el('div', 'gpwarn');
    warn.textContent = 'Unknown event name(s) in the config: ' + unknown.join(', ');
    box.appendChild(warn);
  }
}

$('#gp-save').addEventListener('click', async () => {
  const msg = $('#gp-msg');
  msg.textContent = 'saving…';
  msg.style.color = 'var(--dim)';

  const disabled = [];
  document.querySelectorAll('#gp-raids [data-event]').forEach(cb => {
    if (cb.checked) disabled.push(cb.dataset.event);
  });

  try {
    const d = await post('/api/v1/gameplay', {
      autofeed: {
        enabled: $('#gp-af-enabled').checked,
        debug: $('#gp-af-debug').checked,
        sign_prefix: $('#gp-af-prefix').value,
        sign_radius: Number($('#gp-af-signradius').value),
        range: Number($('#gp-af-range').value),
        interval_seconds: Number($('#gp-af-interval').value),
      },
      raids: {
        enabled: $('#gp-raid-enabled').checked,
        disabled,
      },
    });
    msg.style.color = 'var(--good)';
    msg.textContent = d.message || 'saved';
    setTimeout(loadGameplay, 2500);
  } catch (e) {
    msg.style.color = 'var(--bad)';
    msg.textContent = e.message;
  }
});

/* ---------- boot ---------- */
(async () => {
  await loadSession();
  refresh();
  setInterval(refresh, 5000);
})();
