'use strict';

/* Valheim live map.
 *
 * The panel serves the agent's *data* layers (biome / height / forest / mask),
 * not a picture. Styling happens here: a canvas does 4M pixels in milliseconds,
 * where doing it server-side in pure Python would take seconds.
 *
 * Layer encoding (agent contract):
 *   R = mask nibble (high) | biome code (low)
 *   G:B = (height + 200) * 32, big endian
 *   A = forest factor * 100
 *
 * Image mapping: u = (x + R) / 2R, v = (R - z) / 2R  with R = world_radius,
 * north up.
 */

const BIOME = {
  0:  { name: 'Off-world',  color: [12, 14, 22] },
  1:  { name: 'Meadows',    color: [124, 152, 84] },
  2:  { name: 'Black Forest', color: [58, 82, 58] },
  3:  { name: 'Swamp',      color: [64, 74, 58] },
  4:  { name: 'Mountain',   color: [126, 128, 132] },
  5:  { name: 'Plains',     color: [166, 158, 88] },
  6:  { name: 'Mistlands',  color: [86, 92, 104] },
  7:  { name: 'Ashlands',   color: [104, 66, 58] },
  8:  { name: 'Deep North', color: [198, 206, 214] },
  9:  { name: 'Ocean',      color: [38, 68, 104] },
  15: { name: 'Edge',       color: [10, 12, 18] },
};

const mapState = {
  canvas: null,
  ctx: null,
  meta: null,
  layers: null,        // ImageData of the decoded layers
  fog: null,           // ImageData of the fog mask
  fogCanvas: null,
  fogOverlay: null,    // cached canvas: fog painted for compositing
  styled: null,        // offscreen canvas holding the styled world
  styledSize: 0,
  view: { scale: 1, ox: 0, oy: 0 },   // pan/zoom in canvas pixels
  players: [],
  objects: [],
  locations: [],
  pins: [],
  showFog: true,
  showObjects: true,
  showPlayers: true,
  scopeAll: false,     // admin only: lift fog and show the whole world
  scopeInitialised: false,
  textStep: 0,         // index into TEXT_STEPS
  ready: false,
  hover: null,
};

/* Label size steps. The old fixed 10/11px is the smallest entry, so nothing that
 * already looked right gets bigger unless the viewer asks. */
const TEXT_STEPS = [
  { name: 'small',   scale: 1.0 },
  { name: 'normal',  scale: 1.3 },
  { name: 'large',   scale: 1.7 },
  { name: 'x-large', scale: 2.2 },
];
const TEXT_STORE_KEY = 'valheim-panel.mapTextStep';
const SCOPE_STORE_KEY = 'valheim-panel.mapScopeAll';

let mapInitDone = false;

/* ---------- loading ---------- */
async function initMap() {
  const canvas = document.getElementById('map-canvas');
  if (!canvas) return;
  mapState.canvas = canvas;
  mapState.ctx = canvas.getContext('2d');

  if (!mapInitDone) {
    bindMapControls();
    mapInitDone = true;
  }

  try {
    const r = await fetch('/api/v1/map', { cache: 'no-store' });
    const meta = await r.json();
    if (!r.ok || !meta.layers_ready) {
      showMapMessage(meta.error || 'Map layers are not ready yet. The server samples them shortly after the world loads.');
      return;
    }
    mapState.meta = meta;
    hideMapMessage();

    if (!mapState.layers) {
      await loadLayerImage();
    }
    await loadFog();
    await refreshMapEntities();
    buildStyled();
    fitMap();
    applyFogDefault();
    drawMap();
    updateMapStats();
  } catch (e) {
    showMapMessage('Could not load the map: ' + e.message);
  }
}

function showMapMessage(text) {
  const el = document.getElementById('map-msg');
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
}
function hideMapMessage() {
  const el = document.getElementById('map-msg');
  if (el) el.hidden = true;
}

function loadLayerImage() {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const c = document.createElement('canvas');
      c.width = img.width;
      c.height = img.height;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0);
      mapState.layers = ctx.getImageData(0, 0, img.width, img.height);
      resolve();
    };
    img.onerror = () => reject(new Error('layers image failed to decode'));
    // Cache-bust only on seed change; the server sets immutable caching.
    img.src = '/api/v1/map/layers.png?seed=' + (mapState.meta?.seed ?? 0);
  });
}

function loadFog() {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const c = document.createElement('canvas');
      c.width = img.width;
      c.height = img.height;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0);
      mapState.fog = ctx.getImageData(0, 0, img.width, img.height);
      mapState.fogCanvas = c;
      buildFogOverlay();
      resolve();
    };
    img.onerror = () => { mapState.fog = null; resolve(); };
    img.src = '/api/v1/map/fog.png?t=' + Math.floor(Date.now() / 10000);
  });
}

async function refreshMapEntities() {
  try {
    // `scope=all` is only honoured for admins; the server ignores it otherwise,
    // so sending it unconditionally would be safe but misleading in the URL.
    const qs = mapState.scopeAll ? '?scope=all' : '';
    const r = await fetch('/api/v1/map' + qs, { cache: 'no-store' });
    if (!r.ok) return;
    const d = await r.json();
    mapState.players = d.players || [];
    mapState.objects = d.objects || [];
    mapState.locations = d.locations || [];
    mapState.pins = d.pins || [];
    mapState.meta = { ...mapState.meta, ...d };

    // The scope switch is an admin affordance; hide it for everyone else rather
    // than showing a control that would be silently ignored.
    const wrap = document.getElementById('map-scope-wrap');
    if (wrap) wrap.hidden = !d.can_see_all;
    const cb = document.getElementById('map-scope-all');
    if (cb) cb.checked = !!d.fog_lifted;

    // Admins keep seeing the whole world by default — that is the behaviour the
    // panel had before the explored-only scope existed. An explicit choice is
    // remembered per browser, so switching to "explored" sticks.
    if (d.can_see_all && !mapState.scopeInitialised) {
      mapState.scopeInitialised = true;
      let saved = null;
      try { saved = localStorage.getItem(SCOPE_STORE_KEY); } catch (_) {}
      if (saved === null) {
        mapState.scopeAll = true;
        if (cb) cb.checked = true;
        await refreshMapEntities();
        return;
      }
      mapState.scopeAll = saved === 'true';
      if (cb) cb.checked = mapState.scopeAll;
      if (mapState.scopeAll !== !!d.fog_lifted) {
        await refreshMapEntities();
        return;
      }
    }
  } catch (_) { /* keep previous */ }
}

/* ---------- styling ---------- */
function buildStyled() {
  const L = mapState.layers;
  if (!L) return;

  const w = L.width, h = L.height;
  const src = L.data;
  const out = new ImageData(w, h);
  const dst = out.data;
  const R = mapState.meta?.world_radius || 10500;
  const playable = mapState.meta?.playable_radius || 10000;
  const sea = mapState.meta?.sea_level ?? 30;

  // Height is needed for shading; keep a Float32 copy for the neighbour lookups.
  const heights = new Float32Array(w * h);
  for (let i = 0, p = 0; i < src.length; i += 4, p++) {
    const g = src[i + 1], b = src[i + 2];
    heights[p] = ((g << 8) | b) / 32 - 200;
  }

  const cx = w / 2, cy = h / 2;

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const p = y * w + x;
      const i = p * 4;
      const rv = src[i];
      const biome = rv & 0x0F;
      const mask = rv >> 4;
      const forest = src[i + 3] / 100;
      const hgt = heights[p];

      const b = BIOME[biome] || BIOME[0];
      let cr = b.color[0], cg = b.color[1], cb = b.color[2];

      // Ocean and off-world: depth-shade by how far below sea level.
      // Water: a clear blue ramp so it reads as water at a glance and stays
      // distinct from every land biome. Shallow water is a light teal, deepening
      // to navy, with a pale shoreline band just above sea level.
      if (biome === 9) {
        const depth = Math.min(1, Math.max(0, (sea - hgt) / 120));
        // shallow (86,160,196) -> deep (14,34,76)
        cr = 86 - depth * 72;
        cg = 160 - depth * 126;
        cb = 196 - depth * 120;
        // Keep a hint of blue even in the deepest trench.
        cb = Math.max(70, cb);
      } else if (biome === 0 || biome === 15) {
        cr = 10; cg = 12; cb = 18;
      } else {
        // Forest darkens the canopy; low forest factor means dense trees.
        const shade = 1 - Math.min(0.45, Math.max(0, (1 - forest) * 0.42));
        cr *= shade; cg *= shade; cb *= shade;

        // Snow above the tree line.
        if (biome === 4 && hgt > 150) {
          const t = Math.min(1, (hgt - 150) / 90);
          cr += (232 - cr) * t; cg += (238 - cg) * t; cb += (244 - cb) * t;
        }

        // Beach band: the first few metres above sea level read as sand, which
        // makes the coastline legible against both water and grass.
        if (hgt >= sea && hgt < sea + 6) {
          const t = 1 - (hgt - sea) / 6;
          cr += (206 - cr) * t * 0.85;
          cg += (192 - cg) * t * 0.85;
          cb += (142 - cb) * t * 0.85;
        }

        // Hill shading, lit from the north-west, central differences.
        const xm = x > 0 ? heights[p - 1] : hgt;
        const xp = x < w - 1 ? heights[p + 1] : hgt;
        const ym = y > 0 ? heights[p - w] : hgt;
        const yp = y < h - 1 ? heights[p + w] : hgt;
        const dzdx = (xp - xm) * 0.5;
        const dzdy = (yp - ym) * 0.5;
        const lit = (-dzdx * 0.7 + dzdy * 0.7) * 0.09;
        const k = Math.max(0.55, Math.min(1.45, 1 + lit));
        cr *= k; cg *= k; cb *= k;
      }

      // Darken the ring beyond the playable radius.
      const dx = x - cx, dy = y - cy;
      const distM = Math.sqrt(dx * dx + dy * dy) * (2 * R / w);
      if (distM > playable) {
        const t = Math.min(0.75, (distM - playable) / 900);
        cr *= (1 - t); cg *= (1 - t); cb *= (1 - t);
      }

      // Mask nibble: lava / mist overlays the generator reported.
      if (mask) {
        if (biome === 7 && mask > 0) { cr += 60 * Math.min(1, mask / 4); cg += 14; }
        if (biome === 6 && mask > 0) { cr += 12; cg += 14; cb += 20; }
      }

      dst[i] = cr < 0 ? 0 : cr > 255 ? 255 : cr;
      dst[i + 1] = cg < 0 ? 0 : cg > 255 ? 255 : cg;
      dst[i + 2] = cb < 0 ? 0 : cb > 255 ? 255 : cb;
      dst[i + 3] = 255;
    }
  }

  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  c.getContext('2d').putImageData(out, 0, 0);
  mapState.styled = c;
  mapState.styledSize = w;
}

/* Fog defaults off when nothing has been explored yet.
 *
 * The agent reconstructs exploration from player positions and cartography-table
 * data. On a world it has only just started watching, the mask is 255 everywhere,
 * so honouring it would show a completely black square — technically accurate and
 * completely useless. Turn the overlay off in that case and say why, rather than
 * pretending there is fog data or silently hiding the terrain.
 */
function applyFogDefault() {
  const pct = mapState.meta?.fog?.explored_percent;
  const cb = document.getElementById('map-fog');
  const note = document.getElementById('map-fognote');

  if (pct === 0) {
    if (cb) cb.checked = false;
    mapState.showFog = false;
    if (note) {
      note.hidden = false;
      note.textContent =
        'No exploration recorded yet — the server reconstructs fog from player '
        + 'positions and cartography tables, so it fills in as people play. Fog is '
        + 'off so the terrain is visible.';
    }
  } else {
    if (cb) cb.checked = true;
    mapState.showFog = true;
    if (note) note.hidden = true;
  }
}

/* ---------- drawing ---------- */
function fitMap() {
  const cv = mapState.canvas;
  if (!cv) return;
  const rect = cv.getBoundingClientRect();
  // A hidden view reports a zero-sized rect; sizing the canvas to that leaves a
  // 1x1 bitmap and the map appears blank even after the tab becomes visible.
  if (rect.width < 2 || rect.height < 2) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.floor(rect.width * dpr));
  cv.height = Math.max(1, Math.floor(rect.height * dpr));
  mapState.view.scale = Math.min(cv.width, cv.height);
  mapState.view.ox = (cv.width - mapState.view.scale) / 2;
  mapState.view.oy = (cv.height - mapState.view.scale) / 2;
}

function worldToScreen(x, z) {
  const R = mapState.meta?.world_radius || 10500;
  const { scale, ox, oy } = mapState.view;
  const u = (x + R) / (2 * R);
  const v = (R - z) / (2 * R);
  return [ox + u * scale, oy + v * scale];
}

/* Fog overlay is cached: rebuilding a 1M-pixel ImageData on every redraw made
 * panning crawl, because drawMap() runs on each mousemove. It only changes when a
 * new fog image is loaded. */
function buildFogOverlay() {
  if (!mapState.fog) { mapState.fogOverlay = null; return; }
  const fw = mapState.fog.width, fh = mapState.fog.height;
  const fd = mapState.fog.data;
  const tmp = document.createElement('canvas');
  tmp.width = fw; tmp.height = fh;
  const tctx = tmp.getContext('2d');
  const out = new ImageData(fw, fh);
  const od = out.data;
  for (let i = 0, p = 0; i < fd.length; i += 4, p += 4) {
    const g = fd[i];
    od[p] = 8; od[p + 1] = 10; od[p + 2] = 14;
    od[p + 3] = Math.max(0, Math.min(255, g * 0.92));
  }
  tctx.putImageData(out, 0, 0);
  mapState.fogOverlay = tmp;
}

function drawMap() {
  const cv = mapState.canvas, ctx = mapState.ctx;
  if (!cv || !ctx || !mapState.styled) return;

  const { scale, ox, oy } = mapState.view;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(mapState.styled, ox, oy, scale, scale);

  // Fog: 255 = unexplored, painted over the styled map from the cached overlay.
  if (mapState.showFog && mapState.fogOverlay) {
    ctx.globalAlpha = 0.94;
    ctx.drawImage(mapState.fogOverlay, ox, oy, scale, scale);
    ctx.globalAlpha = 1;
  }

  if (mapState.showObjects) {
    drawObjects(ctx);
  }

  if (mapState.showPlayers) {
    drawPlayers(ctx);
  }
}

function textScale() {
  const step = TEXT_STEPS[mapState.textStep] || TEXT_STEPS[0];
  return step.scale;
}

function drawObjects(ctx) {
  const dpr = window.devicePixelRatio || 1;
  const ts = textScale();
  const draw = (list, color, radius, label) => {
    list.forEach(o => {
      if (o.x == null || o.z == null) return;
      const [sx, sy] = worldToScreen(o.x, o.z);
      ctx.beginPath();
      ctx.arc(sx, sy, radius * dpr, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = '#000000aa';
      ctx.lineWidth = 1 * dpr;
      ctx.stroke();
      if (label && mapState.view.scale > 900 * dpr) {
        const text = o.text || o.label || '';
        if (!text) return;
        ctx.font = `${10 * ts * dpr}px ui-monospace, monospace`;
        // Halo first, then the text: labels sit on terrain of any colour, and a
        // thin dark outline is what keeps them readable over snow and water.
        ctx.lineWidth = 3 * dpr;
        ctx.strokeStyle = 'rgba(0,0,0,0.75)';
        ctx.strokeText(text, sx + 6 * ts * dpr, sy + 3 * ts * dpr);
        ctx.fillStyle = '#f2ece0';
        ctx.fillText(text, sx + 6 * ts * dpr, sy + 3 * ts * dpr);
      }
    });
  };

  draw(mapState.locations, '#d8a24a', 3.5, true);
  draw(mapState.pins, '#6aa9d8', 3, true);

  const portals = mapState.objects.filter(o => o.type === 'portal');
  draw(portals, '#b48ead', 3, true);

  const rest = mapState.objects.filter(o => o.type !== 'portal');
  draw(rest, '#8a8f98', 2.2, false);
}

function drawPlayers(ctx) {
  const dpr = window.devicePixelRatio || 1;
  const ts = textScale();
  mapState.players.forEach(p => {
    if (p.x == null || p.z == null) return;
    const [sx, sy] = worldToScreen(p.x, p.z);

    ctx.beginPath();
    ctx.arc(sx, sy, 5 * dpr, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.strokeStyle = '#d8a24a';
    ctx.lineWidth = 2 * dpr;
    ctx.stroke();

    const label = p.name || '?';
    const x = sx + 9 * ts * dpr;
    const y = sy + 4 * ts * dpr;
    ctx.font = `600 ${11 * ts * dpr}px ui-sans-serif, system-ui, sans-serif`;
    ctx.lineWidth = 3.5 * dpr;
    ctx.strokeStyle = 'rgba(0,0,0,0.8)';
    ctx.strokeText(label, x, y);
    ctx.fillStyle = '#f4ecdf';
    ctx.fillText(label, x, y);
  });
}

/* ---------- stats ---------- */
function updateMapStats() {
  const m = mapState.meta || {};
  const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  set('map-seed', m.seed ?? '—');
  set('map-players', (mapState.players || []).length);
  const fog = m.fog || {};
  set('map-explored', fog.explored_percent != null ? fog.explored_percent + '%' : '—');
  set('map-objects', (mapState.objects || []).length);
  set('map-locations', (mapState.locations || []).length);
}

/* ---------- interaction ---------- */
function bindMapControls() {
  const cv = mapState.canvas;

  let dragging = false, lastX = 0, lastY = 0;

  cv.addEventListener('mousedown', e => {
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    cv.style.cursor = 'grabbing';
  });
  window.addEventListener('mouseup', () => {
    dragging = false;
    if (cv) cv.style.cursor = 'grab';
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const dpr = window.devicePixelRatio || 1;
    mapState.view.ox += (e.clientX - lastX) * dpr;
    mapState.view.oy += (e.clientY - lastY) * dpr;
    lastX = e.clientX; lastY = e.clientY;
    drawMap();
  });

  cv.addEventListener('wheel', e => {
    e.preventDefault();
    const dpr = window.devicePixelRatio || 1;
    const rect = cv.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * dpr;
    const my = (e.clientY - rect.top) * dpr;

    const factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    const v = mapState.view;
    const newScale = Math.max(120 * dpr, Math.min(12000 * dpr, v.scale * factor));
    const k = newScale / v.scale;
    v.ox = mx - (mx - v.ox) * k;
    v.oy = my - (my - v.oy) * k;
    v.scale = newScale;
    drawMap();
  }, { passive: false });

  cv.addEventListener('mousemove', e => {
    if (dragging) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = cv.getBoundingClientRect();
    const mx = (e.clientX - rect.left) * dpr;
    const my = (e.clientY - rect.top) * dpr;
    const R = mapState.meta?.world_radius || 10500;
    const v = mapState.view;
    const u = (mx - v.ox) / v.scale;
    const vv = (my - v.oy) / v.scale;
    if (u < 0 || u > 1 || vv < 0 || vv > 1) {
      setMapHover('');
      return;
    }
    const x = u * 2 * R - R;
    const z = R - vv * 2 * R;
    setMapHover(`${Math.round(x)}, ${Math.round(z)}`);
  });

  cv.style.cursor = 'grab';

  window.addEventListener('resize', () => {
    if (!mapState.styled) return;
    fitMap();
    drawMap();
  });

  const fogCb = document.getElementById('map-fog');
  if (fogCb) fogCb.addEventListener('change', e => {
    mapState.showFog = e.target.checked; drawMap();
  });
  const objCb = document.getElementById('map-objects-toggle');
  if (objCb) objCb.addEventListener('change', e => {
    mapState.showObjects = e.target.checked; drawMap();
  });
  const plCb = document.getElementById('map-players-toggle');
  if (plCb) plCb.addEventListener('change', e => {
    mapState.showPlayers = e.target.checked; drawMap();
  });

  const reset = document.getElementById('map-reset');
  if (reset) reset.addEventListener('click', () => { fitMap(); drawMap(); });

  // ---- text size ----
  const applyTextStep = () => {
    const step = TEXT_STEPS[mapState.textStep] || TEXT_STEPS[0];
    const label = document.getElementById('map-text-label');
    if (label) label.textContent = step.name;
    const smaller = document.getElementById('map-text-smaller');
    const bigger = document.getElementById('map-text-bigger');
    if (smaller) smaller.disabled = mapState.textStep <= 0;
    if (bigger) bigger.disabled = mapState.textStep >= TEXT_STEPS.length - 1;
    try { localStorage.setItem(TEXT_STORE_KEY, String(mapState.textStep)); } catch (_) {}
    drawMap();
  };

  const smaller = document.getElementById('map-text-smaller');
  if (smaller) smaller.addEventListener('click', () => {
    mapState.textStep = Math.max(0, mapState.textStep - 1);
    applyTextStep();
  });
  const bigger = document.getElementById('map-text-bigger');
  if (bigger) bigger.addEventListener('click', () => {
    mapState.textStep = Math.min(TEXT_STEPS.length - 1, mapState.textStep + 1);
    applyTextStep();
  });

  // Restore the viewer's last choice.
  try {
    const saved = parseInt(localStorage.getItem(TEXT_STORE_KEY) || '0', 10);
    if (!Number.isNaN(saved)) {
      mapState.textStep = Math.max(0, Math.min(TEXT_STEPS.length - 1, saved));
    }
  } catch (_) {}
  applyTextStep();

  // ---- admin scope ----
  const scopeAll = document.getElementById('map-scope-all');
  if (scopeAll) scopeAll.addEventListener('change', async e => {
    mapState.scopeAll = e.target.checked;
    try { localStorage.setItem(SCOPE_STORE_KEY, String(mapState.scopeAll)); } catch (_) {}
    await refreshMapEntities();
    drawMap();
    updateMapStats();
  });
}

function setMapHover(text) {
  const e = document.getElementById('map-hover');
  if (e) e.textContent = text;
}

/* Live refresh: players and fog move, the terrain does not. */
setInterval(async () => {
  if (currentView !== 'map' || !mapState.styled) return;
  await refreshMapEntities();
  await loadFog();
  drawMap();
  updateMapStats();
}, 5000);
