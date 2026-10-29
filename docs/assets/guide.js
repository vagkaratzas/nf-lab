// nf-lab guide: one plate's journey from a Nextflow pipeline to a simulated robot and back.
// No dependencies. The plate map, the idempotency key and the Ct values are computed as nf-lab
// computes them; timings, job IDs and task hashes are illustrative.
(() => {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function el(tag, attrs = {}, parent, text) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  }

  // ---------------------------------------------------------------- plate Q01
  // As examples/qpcr/plate_map.csv has it: samples down the columns, controls in A12-D12.
  const ROWS = 'ABCDEFGH'.split('');
  const CONTROLS = {
    A12: ['NTC-Q01-A12', 'negative_control'], B12: ['NTC-Q01-B12', 'negative_control'],
    C12: ['POS-Q01-C12', 'positive_control'], D12: ['BLK-Q01-D12', 'blank'],
  };
  const WELLS = [];
  for (let c = 1, n = 0; c <= 12; c++) {
    for (const r of ROWS) {
      const ctl = CONTROLS[r + c];
      WELLS.push(ctl ? { row: r, column: c, sample: ctl[0], type: ctl[1] }
        : { row: r, column: c, sample: 'S' + String(++n).padStart(4, '0'), type: 'sample' });
    }
  }

  const CRC = new Uint32Array(256).map((_, i) => {
    let c = i;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  function crc32(s) {
    let c = 0xFFFFFFFF;
    for (const b of new TextEncoder().encode(s)) c = CRC[(c ^ b) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }
  // The simulated qPCR read (the adapter's bindings `ct`): the positive control at 25.0, no Ct for the
  // other controls, and each sample its own value from its ID, about one in seven undetermined.
  function ct(w) {
    if (w.type === 'positive_control') return 25.0;
    const h = crc32(`${w.sample}:ct`);
    if (w.type !== 'sample' || h % 7 === 0) return null;
    return Math.round((16 + (h % 1800) / 100) * 100) / 100;
  }
  for (const w of WELLS) { w.ct = ct(w); w.id = w.row + w.column; }
  console.assert(WELLS[0].sample === 'S0001' && WELLS[0].ct === 17.02, 'ct() no longer matches the adapter');
  console.assert(WELLS.filter(w => w.type === 'sample').length === 92, 'plate Q01 should hold 92 samples');

  // labctl's default key: sha256 of json.dumps([protocol, version, plate, wells(, params)]), Python's separators.
  const pyjson = v => Array.isArray(v) ? `[${v.map(pyjson).join(', ')}]`
    : v !== null && typeof v === 'object' ? `{${Object.keys(v).map(k => `${JSON.stringify(k)}: ${pyjson(v[k])}`).join(', ')}}`
    : JSON.stringify(v);
  const keyPayload = params => {
    const wells = [...WELLS].sort((a, b) => a.row < b.row ? -1 : a.row > b.row ? 1 : a.column - b.column)
      .map(({ row, column, sample, type }) => ({ row, column, sample, type }));
    return pyjson(['qpcr', '1.0', 'Q01', wells, ...(params ? [params] : [])]);
  };
  // Computed with labctl's own function; the page recomputes them and checks.
  const KEYS = {
    plain: '85cf0c65b126fd8e1acf0dc0f6293a94b1f79c9e8f2623e8d717cf244d93acbf',
    cycles45: 'dd7cbc900e8744fd077dbc3ffec365bc32437b350392502fc1838ac7096ffaed',
  };
  let keysLive = false;
  (async () => {
    try {
      const hex = async s => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)))]
        .map(b => b.toString(16).padStart(2, '0')).join('');
      const plain = await hex(keyPayload(null));
      const cycles45 = await hex(keyPayload({ cycles: 45 }));
      console.assert(plain === KEYS.plain && cycles45 === KEYS.cycles45, 'key no longer matches labctl', plain, cycles45);
      Object.assign(KEYS, { plain, cycles45 });
      keysLive = true;
      render();
    } catch (e) { /* no WebCrypto (plain http): keep the precomputed keys */ }
  })();

  // Illustrative IDs, stable across visits.
  const hexOf = (seed, len) => {
    let s = '';
    for (let i = 0; s.length < len; i++) s += crc32(`${seed}:${i}`).toString(16).padStart(8, '0');
    return s.slice(0, len);
  };
  const jobId = n => hexOf(`job-${n}`, 32);
  const taskHash = n => hexOf(`task-${n}`, 32);
  const nfHash = h => `${h.slice(0, 2)}/${h.slice(2, 8)}`;

  // ---------------------------------------------------------------- stations and the belt
  const ST = [
    { n: 1, name: 'Pipeline', zone: 'nf', x: 90, y: 150, icon: 'dag' },
    { n: 2, name: 'Wet module', zone: 'nf', x: 220, y: 150, icon: 'module' },
    { n: 3, name: 'Lab executor', zone: 'nf', x: 350, y: 150, icon: 'exec' },
    { n: 4, name: 'labctl', zone: 'nf', x: 480, y: 150, icon: 'term' },
    { n: 5, name: 'Contract', zone: 'contract', x: 600, y: 150, icon: 'doc' },
    { n: 6, name: 'Ledger', zone: 'lab', x: 720, y: 150, icon: 'db' },
    { n: 7, name: 'Pools', zone: 'lab', x: 850, y: 150, icon: 'pool' },
    { n: 8, name: 'Deck page', zone: 'lab', x: 980, y: 150, icon: 'person' },
    { n: 9, name: 'Binding', zone: 'lab', x: 1110, y: 350, icon: 'plug', tile: 'side' },
    { n: 10, name: 'qpcr-1', zone: 'lab', x: 960, y: 560, icon: 'robot' },
    { n: 11, name: 'Results', zone: 'nf', x: 480, y: 560, icon: 'back' },
    { n: 12, name: 'Dry calls', zone: 'nf', x: 350, y: 560, icon: 'chart' },
    { n: 13, name: 'Lab report', zone: 'nf', x: 220, y: 560, icon: 'report' },
    { n: 14, name: 'Lineage', zone: 'nf', x: 90, y: 560, icon: 'tree' },
  ];
  const GATE = { x: 600, y: 560 };
  const PATH = [...ST.slice(0, 8), { x: 1110, y: 150 }, ST[8], { x: 1110, y: 560 }, ST[9], GATE, ...ST.slice(10)];
  PATH.forEach((p, i) => { p.d = i ? PATH[i - 1].d + Math.hypot(p.x - PATH[i - 1].x, p.y - PATH[i - 1].y) : 0; });
  const LAST = PATH[PATH.length - 1].d;
  function pointAt(d) {
    if (d <= 0) return { x: PATH[0].x, y: PATH[0].y };
    for (let i = 1; i < PATH.length; i++) {
      const a = PATH[i - 1], b = PATH[i];
      if (d <= b.d) {
        const t = (d - a.d) / (b.d - a.d || 1);
        return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
      }
    }
    return { x: PATH[PATH.length - 1].x, y: PATH[PATH.length - 1].y };
  }
  const stationD = i => (i <= 0 ? 0 : ST[i - 1].d);

  // ---------------------------------------------------------------- state
  const fresh = () => ({
    idx: 0,               // 0: before the start; 1-14: the station the plate is at
    visited: new Set(),
    d: 0,                 // the plate's distance along the belt
    rerun: false,         // the second lap: the same run with -resume
    runs: 1,              // nextflow launches in the session
    params45: false,
    loaded: false,        // Q01 is on qpcr-1's deck
    unloaded: false,
    attempts: [],         // the ledger's rows for Q01's key
    exit: null,           // labctl's exit code in the latest launch
    needsResume: false,   // the launch ended without collecting the job
    lateAbort: false,
    cycle: 0,
    sim: null,
    playing: false,
  });
  let S = fresh();
  const job = () => S.attempts[S.attempts.length - 1];
  const collected = () => !!job() && job().state === 'done' && S.exit === 0;

  // ---------------------------------------------------------------- drawing the stage
  const stage = $('#stage');
  const ICONS = {
    dag: '<path d="M11 20L20 11M11 20L20 29M20 11L29 20M20 29L29 20"/><circle cx="8" cy="20" r="3.5"/><circle cx="20" cy="9" r="3.5"/><circle cx="20" cy="31" r="3.5"/><circle cx="32" cy="20" r="3.5"/>',
    module: '<rect x="7" y="9" width="26" height="22" rx="3"/><path d="M3 20h4M33 20h4M12 16h16M12 21h10M12 26h13"/>',
    exec: '<circle cx="13" cy="20" r="6"/><path d="M19 20h15M30 16l4 4-4 4M17 15l9-7M23 8h3v3M17 25l9 7M23 32h3v-3"/>',
    term: '<rect x="4" y="8" width="32" height="24" rx="3"/><path d="M10 16l5 4-5 4M18 25h9"/>',
    doc: '<path d="M10 5h14l7 7v23H10z"/><path d="M24 5v7h7M14 19h13M14 24h13M14 29h8"/>',
    db: '<ellipse cx="20" cy="10" rx="12" ry="4"/><path d="M8 10v20c0 2.2 5.4 4 12 4s12-1.8 12-4V10M8 20c0 2.2 5.4 4 12 4s12-1.8 12-4"/>',
    pool: '<rect x="3" y="17" width="10" height="14" rx="2"/><rect x="15" y="17" width="10" height="14" rx="2"/><rect x="27" y="17" width="10" height="14" rx="2"/><path d="M6 10h28M30 6l4 4-4 4"/>',
    person: '<circle cx="13" cy="12" r="4.5"/><path d="M4 33c0-6 4-11 9-11s9 5 9 11"/><rect x="24" y="13" width="13" height="10" rx="1.5"/><path d="M30.5 23v5M27 28h7"/>',
    plug: '<path d="M15 5v8M25 5v8M10 13h20v6a10 10 0 0 1-20 0zM20 29v7"/>',
    robot: '<path d="M9 15l4-7h14l4 7"/><rect x="6" y="15" width="28" height="18" rx="2"/><rect x="10" y="20" width="12" height="7" rx="1"/><circle cx="28" cy="23.5" r="2"/>',
    back: '<path d="M33 12H17a8 8 0 0 0 0 16h4"/><path d="M11 8l-4 4 4 4M7 12h10"/><rect x="23" y="22" width="11" height="12" rx="1.5"/>',
    chart: '<path d="M5 34h30"/><rect x="9" y="21" width="5" height="13"/><rect x="17.5" y="12" width="5" height="22"/><rect x="26" y="17" width="5" height="17"/>',
    report: '<rect x="9" y="7" width="22" height="28" rx="2"/><path d="M15 7V4h10v3M14 20l4 4 8-8M14 30h12"/>',
    tree: '<path d="M20 12v6M20 18L10 26M20 18l10 8"/><circle cx="20" cy="8" r="3.5"/><circle cx="20" cy="18" r="1.5"/><circle cx="10" cy="29" r="3.5"/><circle cx="30" cy="29" r="3.5"/>',
  };

  const layers = {};
  function drawStage() {
    const defs = el('defs', {}, stage);
    const grad = el('linearGradient', { id: 'shine', x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    el('stop', { offset: 0, 'stop-color': '#ffffff', 'stop-opacity': 0.08 }, grad);
    el('stop', { offset: 1, 'stop-color': '#ffffff', 'stop-opacity': 0 }, grad);

    el('rect', { class: 'zone-nf', x: 10, y: 10, width: 588, height: 670, rx: 14 }, stage);
    el('rect', { class: 'zone-lab', x: 602, y: 10, width: 588, height: 670, rx: 14 }, stage);
    el('text', { class: 'zone-title nf', x: 28, y: 30 }, stage, 'NEXTFLOW · YOUR PIPELINE');
    el('text', { class: 'zone-title lab', x: 1172, y: 30, 'text-anchor': 'end' }, stage, 'THE LAB · DEVICE ADAPTER');
    el('line', { class: 'boundary', x1: 600, y1: 16, x2: 600, y2: 674 }, stage);
    el('text', { class: 'boundary-label', transform: 'translate(612 300) rotate(90)' }, stage, 'HTTP · OPENAPI.JSON');

    drawTerminal();
    drawBay();

    const pts = PATH.map(p => `${p.x},${p.y}`).join(' ');
    el('polyline', { class: 'belt', points: pts }, stage);
    el('polyline', { class: 'belt-stripes', points: pts }, stage);
    el('path', { class: 'return', d: `M${ST[13].x} ${ST[13].y - 28}V${ST[0].y + 28}` }, stage);
    el('path', { class: 'return', d: `M${ST[0].x - 6} ${ST[0].y + 38}l6 -8 6 8` }, stage);
    el('text', { class: 'return-label', transform: `translate(${ST[0].x - 10} 420) rotate(-90)` }, stage, 'next launch · -resume');

    const gate = el('g', {}, stage);
    el('rect', { class: 'gate', x: GATE.x - 26, y: GATE.y - 24, width: 52, height: 48, rx: 8 }, gate);
    el('text', { class: 'gate-text', x: GATE.x, y: GATE.y - 32 }, gate, 'status: done');

    layers.stations = el('g', {}, stage);
    ST.forEach((s, i) => drawStation(s, i + 1));
    layers.leftover = makePlate(el('g', { opacity: 0 }, stage), true);
    layers.ghost = makePlate(el('g', { class: 'ghost', opacity: 0 }, stage));
    layers.plate = makePlate(el('g', {}, stage));
  }

  function drawStation(s, i) {
    const top = s.y < 300;
    const g = el('g', { class: `station ${s.zone === 'nf' ? 'nf' : 'lab'}`, tabindex: 0, role: 'button', 'aria-label': `Station ${i}: ${s.name}`, 'data-i': i }, layers.stations);
    const tw = 80, th = 56;
    let tx = s.x - tw / 2, ty = top ? s.y - 92 : s.y + 26, ly = top ? ty - 9 : ty + th + 21;
    if (s.tile === 'side') { ty = s.y - 104; ly = s.y + 40; }
    el('rect', { class: 'tile', x: tx, y: ty, width: tw, height: th, rx: 10 }, g);
    el('rect', { x: tx + 1, y: ty + 1, width: tw - 2, height: th / 2, rx: 9, fill: 'url(#shine)' }, g);
    const ic = el('g', { class: 'icon', transform: `translate(${s.x - 20} ${ty + 8})`, fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, g);
    ic.innerHTML = ICONS[s.icon];
    el('text', { class: 'st-label', x: s.x, y: ly }, g, s.name);
    el('circle', { class: 'pulse', cx: s.x, cy: s.y, r: 13 }, g);
    el('circle', { class: 'badge', cx: s.x, cy: s.y, r: 13 }, g);
    el('text', { class: 'badge-n', x: s.x, y: s.y + 0.5 }, g, i);
    g.addEventListener('click', () => go(i));
    g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); go(i); } });
  }

  function makePlate(g, physical = false) {
    el('ellipse', { class: 'plate-shadow', cx: 0, cy: 22, rx: 26, ry: 4 }, g);
    el('rect', { class: 'plate-body', x: -29, y: -19, width: 58, height: 38, rx: 5 }, g);
    const wells = WELLS.map(w => el('circle', {
      cx: -22.5 + (w.column - 1) * 4.1, cy: -13 + ROWS.indexOf(w.row) * 3.75, r: 1.55,
    }, g));
    el('text', { class: 'plate-tag', x: 0, y: -24, 'text-anchor': 'middle' }, g, physical ? 'Q01 (on deck)' : 'Q01');
    return { g, wells };
  }

  function drawTerminal() {
    const g = el('g', {}, stage);
    el('rect', { class: 'term', x: 130, y: 212, width: 440, height: 300, rx: 10 }, g);
    el('path', { class: 'term-bar', d: 'M130 222a10 10 0 0 1 10-10h420a10 10 0 0 1 10 10v12H130z' }, g);
    ['#E5577E', '#F2B84B', '#0DC09D'].forEach((c, i) => el('circle', { cx: 146 + i * 14, cy: 223, r: 4, fill: c }, g));
    el('text', { class: 'term-title', x: 350, y: 227, 'text-anchor': 'middle' }, g, 'head node · nextflow');
    layers.term = el('g', {}, g);
  }

  const POOL = [
    { id: 'qpcr-1', me: true },
    { id: 'qpcr-2', status: 'running', plate: 'Q02' },
    { id: 'qpcr-3', status: 'running', plate: 'Q03' },
    { id: 'qpcr-4', status: 'unload', plate: 'Q04' },
    { id: 'qpcr-5', status: 'running', plate: 'Q05' },
    { id: 'qpcr-6', status: 'ready', plate: 'Q06' },
  ];
  const CH = { x0: 684, x1: 862, y0: 490, y1: 362, thr: 0.12 };
  function drawBay() {
    const g = el('g', {}, stage);
    el('rect', { class: 'panel-bg', x: 640, y: 208, width: 418, height: 106, rx: 10 }, g);
    el('text', { class: 'panel-title', x: 654, y: 228 }, g, 'DECK PAGE · QPCR POOL');
    layers.bots = POOL.map((b, i) => {
      const x = 654 + i * 66, bg = el('g', {}, g);
      el('rect', { class: `bot${b.me ? ' me' : ''}`, x, y: 238, width: 60, height: 66, rx: 7 }, bg);
      el('text', { class: 'bot-name', x: x + 30, y: 254 }, bg, b.id);
      const pill = el('g', {}, bg);
      el('rect', { class: 'pill-bg', x: x + 4, y: 262, width: 52, height: 15 }, pill);
      const pt = el('text', { class: 'pill', x: x + 30, y: 273 }, pill, '');
      const plate = el('text', { class: 'bot-plate', x: x + 30, y: 294 }, bg, '');
      return { b, pill, pt, plate };
    });

    el('rect', { class: 'chart-frame', x: 640, y: 326, width: 236, height: 194, rx: 10 }, g);
    el('text', { class: 'panel-title', x: 654, y: 346 }, g, 'QPCR-1 · FLUORESCENCE · FAM');
    for (let c = 0; c <= 40; c += 10) {
      const x = CH.x0 + (CH.x1 - CH.x0) * c / 40;
      el('line', { class: 'chart-grid', x1: x, y1: CH.y1, x2: x, y2: CH.y0 }, g);
      el('text', { class: 'chart-label', x, y: CH.y0 + 14, 'text-anchor': 'middle' }, g, c);
    }
    el('text', { class: 'chart-label', x: CH.x0 - 6, y: CH.y0 + 14, 'text-anchor': 'end' }, g, 'cycle');
    const ty = CH.y0 - (CH.y0 - CH.y1) * CH.thr;
    el('line', { class: 'threshold', x1: CH.x0, y1: ty, x2: CH.x1, y2: ty }, g);
    el('text', { class: 'chart-label', x: CH.x1, y: ty - 4, 'text-anchor': 'end' }, g, 'threshold');
    layers.curves = WELLS.map(w => el('path', { class: `curve${w.type === 'positive_control' ? ' pos' : w.ct == null ? ' flat' : ''}`, d: '' }, g));
    layers.cycleText = el('text', { class: 'chart-label', x: CH.x0, y: CH.y1 - 2 }, g, '');

    // qpcr-1 itself, above its station on the belt.
    const m = el('g', {}, g);
    layers.machine = m;
    el('rect', { class: 'machine', x: 890, y: 400, width: 150, height: 138, rx: 12 }, m);
    layers.lid = el('rect', { class: 'machine-lid', x: 898, y: 380, width: 134, height: 24, rx: 7 }, m);
    el('rect', { class: 'screen', x: 904, y: 420, width: 98, height: 30, rx: 5 }, m);
    layers.screen = el('text', { class: 'screen-text', x: 912, y: 440 }, m, 'idle');
    layers.led = el('circle', { class: 'led', cx: 1021, cy: 435, r: 6 }, m);
    for (let i = 0; i < 5; i++) el('rect', { x: 906 + i * 25, y: 470, width: 18, height: 4, rx: 2, fill: 'var(--line)' }, m);
    el('text', { class: 'bot-name', x: 965, y: 500 }, m, 'qpcr-1');
    el('text', { class: 'chart-label', x: 965, y: 515, 'text-anchor': 'middle' }, m, 'simulated');
    el('rect', { x: 920, y: 536, width: 90, height: 6, rx: 2, fill: 'var(--line)' }, m);
  }

  // ---------------------------------------------------------------- the device's status
  function deviceStatus() {
    if (S.unloaded || !S.loaded) return 'empty';
    const j = job();
    if (!j) return 'ready';
    if (S.sim) return 'running';
    return { queued: 'ready', failed_preflight: 'ready', done: 'unload', stopped: 'needs_person', voided: 'unload' }[j.state] || 'ready';
  }

  // ---------------------------------------------------------------- wells
  function wellView() {
    if (S.idx < 10 && !S.rerun) return { mode: 'map' };
    if (S.sim || (job() && ['stopped', 'failed_preflight'].includes(job().state))) return { mode: 'cycle', cycle: S.cycle };
    if (job() && job().state === 'done') return { mode: S.idx >= 12 ? 'call' : 'ct' };
    return { mode: 'map' };
  }
  function wellPaint(w, v) {
    const base = { fill: 'var(--well-off)', stroke: 'none', dash: '' };
    if (w.type === 'negative_control') Object.assign(base, { stroke: 'var(--pink)' });
    if (w.type === 'blank') Object.assign(base, { stroke: 'var(--muted)', dash: '1.5 1.5' });
    if (w.type === 'positive_control') Object.assign(base, { stroke: 'var(--amber)' });
    if (w.type === 'sample') base.fill = 'var(--well-sample)';
    const heat = w.ct == null ? null : 0.3 + 0.7 * Math.max(0, Math.min(1, 1 - (w.ct - 16) / 19));
    const lit = c => `rgba(13, 192, 157, ${c.toFixed(2)})`;
    if (v.mode === 'cycle' && w.ct != null && w.ct <= v.cycle) base.fill = w.type === 'positive_control' ? 'var(--amber)' : lit(heat);
    if (v.mode === 'ct' && w.ct != null) base.fill = w.type === 'positive_control' ? 'var(--amber)' : lit(heat);
    if (v.mode === 'call') {
      if (w.type === 'sample') base.fill = w.ct != null && w.ct < 35 ? 'var(--green)' : 'var(--well-off)';
      else if (w.ct != null) base.fill = 'var(--amber)';
    }
    return base;
  }
  function paintWells(circles, sw) {
    const v = wellView();
    WELLS.forEach((w, i) => {
      const p = wellPaint(w, v), c = circles[i];
      c.setAttribute('fill', p.fill);
      c.setAttribute('stroke', p.stroke);
      c.setAttribute('stroke-width', sw);
      c.setAttribute('stroke-dasharray', p.dash);
    });
  }

  // ---------------------------------------------------------------- big plate on the traveller card
  const big = $('#bigplate');
  const bigWells = [];
  function drawBigPlate() {
    el('rect', { x: 12, y: 14, width: 282, height: 188, rx: 12, fill: 'var(--panel-2)', stroke: 'var(--line)' }, big);
    for (let c = 1; c <= 12; c++) el('text', { x: 36 + (c - 1) * 22, y: 30, 'text-anchor': 'middle' }, big, c);
    ROWS.forEach((r, i) => el('text', { x: 22, y: 44 + i * 21.2, 'text-anchor': 'middle' }, big, r));
    const tip = $('#well-tip');
    WELLS.forEach(w => {
      const c = el('circle', { cx: 36 + (w.column - 1) * 22, cy: 41 + ROWS.indexOf(w.row) * 21.2, r: 8.3, tabindex: 0 }, big);
      const show = () => {
        const v = wellView();
        const kind = w.type.replace('_', ' ');
        const known = v.mode === 'ct' || v.mode === 'call' || (v.mode === 'cycle' && w.ct != null && w.ct <= v.cycle);
        const ctText = known ? ` · Ct ${w.ct == null ? 'undetermined' : w.ct.toFixed(2)}` : '';
        const call = v.mode === 'call' && w.type === 'sample' ? ` · ${w.ct != null && w.ct < 35 ? 'detected' : 'not detected'}` : '';
        tip.textContent = `${w.id} · ${w.sample} · ${kind}${ctText}${call}`;
      };
      c.addEventListener('mouseenter', show);
      c.addEventListener('focus', show);
      bigWells.push(c);
    });
    $('#legend').innerHTML = [
      ['var(--well-sample)', 'sample'], ['var(--green)', 'Ct (brighter: earlier)'],
      ['transparent;box-shadow:inset 0 0 0 2px var(--pink)', 'no-template control'],
      ['transparent;box-shadow:inset 0 0 0 2px var(--amber)', 'positive control'],
      ['transparent;box-shadow:inset 0 0 0 2px var(--muted)', 'extraction blank'],
    ].map(([c, t]) => `<span><i style="background:${c}"></i>${t}</span>`).join('');
  }

  // ---------------------------------------------------------------- rendering
  function termLines() {
    const L = [];
    if (S.idx < 1 && !S.rerun) return [['dim', '$ _']];
    const resumed = S.rerun || S.runs > 1;
    L.push(['', `$ nextflow run examples/qpcr${resumed ? ' -resume' : ''}`]);
    L.push(['dim', ' N E X T F L O W  ~  version 26.10.2']);
    L.push(['dim', 'Launching `examples/qpcr/main.nf` [mad_curie]']);
    L.push(['', 'Pipeline is starting! 🚀']);
    if (S.idx >= 3 || S.rerun) {
      L.push(['', 'executor >  lab']);
      L.push(['ok', `[${nfHash(taskHash('x'))}] RT_QPCR:RNA_EXTRACTION (Q11) | 11 of 11${resumed ? ', cached: 11' : ''} ✔`]);
      const j = job(), h = nfHash(taskHash(S.attempts.length || 1));
      let q = '| 0 of 11';
      if (S.rerun) q = '| 11 of 11, cached: 11 ✔';
      else if (collected()) q = '| 1 of 11 ✔';
      else if (S.sim) q = `| 0 of 11 · qpcr-1 ${S.cycle ? `cycle ${Math.floor(S.cycle)}/40` : 'pre-flight'}`;
      else if (j && j.state === 'queued') q = '| 0 of 11 · queued';
      L.push([collected() || S.rerun ? 'ok' : '', `[${h}] RT_QPCR:QPCR (Q01)           ${q}`]);
    }
    if (!S.rerun && S.exit === 143) {
      L.push(['', '^C']);
      L.push(['dim', '# TERM to QPCR (Q01): labctl sends safe_stop, exits 143']);
    }
    if (!S.rerun && S.exit === 1) {
      L.push(['err', "ERROR ~ Error executing process > 'RT_QPCR:QPCR (Q01)'"]);
      L.push(['err', 'Caused by: terminated with an error exit status (1)']);
    }
    if ((collected() && S.idx >= 13) || S.rerun) L.push(['ok', `[${nfHash(taskHash('report'))}] LAB_REPORT | 1 of 1${S.rerun && S.idx < 13 ? '' : ' ✔'}`]);
    if ((collected() && S.idx >= 14) || (S.rerun && S.idx >= 14)) L.push(['', 'Pipeline complete! 👋']);
    return L.slice(-12);
  }

  function renderTerminal() {
    const g = layers.term;
    g.textContent = '';
    const lines = termLines();
    lines.forEach(([cls, t], i) => el('text', { class: `term-line ${cls}`, x: 144, y: 258 + i * 20 }, g, t.length > 60 ? t.slice(0, 59) + '…' : t));
    el('rect', { class: 'term-cursor', x: 144, y: 258 + lines.length * 20 - 11, width: 8, height: 14 }, g);
  }

  function renderBay() {
    const status = deviceStatus();
    for (const b of layers.bots) {
      const st = b.b.me ? status : b.b.status;
      b.pill.setAttribute('class', `s-${st}`);
      b.pt.textContent = st === 'needs_person' ? 'needs person' : st;
      b.plate.textContent = b.b.me ? (st === 'empty' ? (S.rerun ? '' : 'next: Q01') : 'Q01') : b.b.plate;
    }
    const j = job();
    const scr = S.sim ? (S.sim.phase === 'preflight' ? 'pre-flight' : S.sim.phase === 'rt' ? 'RT 50°C' : `cycle ${Math.max(1, Math.floor(S.cycle))}/40`)
      : status === 'needs_person' ? (j.state === 'stopped' ? 'E-STOP' : 'check deck')
      : status === 'unload' ? (j.state === 'voided' ? 'voided' : 'done · Q01')
      : status === 'ready' ? 'Q01 · ready' : 'idle';
    layers.screen.textContent = scr;
    layers.led.setAttribute('class', `led${S.sim ? ' on' : status === 'needs_person' ? ' alarm' : status === 'unload' && j.state === 'done' ? ' done' : ''}`);
    layers.lid.style.transform = status === 'ready' || status === 'unload' || status === 'needs_person' ? 'translateY(-12px)' : '';
    renderCurves();
  }

  function curve(w, i, upTo) {
    if (upTo <= 0) return '';
    const sx = c => CH.x0 + (CH.x1 - CH.x0) * c / 40;
    const sy = f => CH.y0 - (CH.y0 - CH.y1) * f;
    const F = c => w.ct == null ? 0.02 + 0.006 * Math.sin(c * 0.9 + i)
      : 0.02 + 0.86 / (1 + Math.exp(-0.8 * (c - (w.ct + 2.567))));
    let d = `M${sx(0).toFixed(1)} ${sy(F(0)).toFixed(1)}`;
    for (let c = 1; c <= upTo; c++) d += `L${sx(c).toFixed(1)} ${sy(F(c)).toFixed(1)}`;
    if (upTo % 1) d += `L${sx(upTo).toFixed(1)} ${sy(F(upTo)).toFixed(1)}`;
    return d;
  }
  function renderCurves() {
    const v = wellView();
    const upTo = v.mode === 'cycle' ? S.cycle : v.mode === 'ct' || v.mode === 'call' ? 40 : 0;
    layers.curves.forEach((p, i) => p.setAttribute('d', curve(WELLS[i], i, upTo)));
    layers.cycleText.textContent = upTo ? `cycle ${Math.floor(upTo)} / 40` : '';
  }

  function placePlate() {
    const p = pointAt(S.d);
    layers.plate.g.setAttribute('transform', `translate(${p.x.toFixed(1)} ${(p.y - 4).toFixed(1)})`);
    // Past the robot only the plate's data travels; the plate itself stays on qpcr-1's deck.
    const data = S.d > ST[9].d + 0.5 || (S.rerun && S.idx >= 11);
    layers.plate.g.setAttribute('class', `plate${data ? ' data' : ''}`);
    const leftover = data && S.loaded && !S.unloaded;
    layers.leftover.g.setAttribute('opacity', leftover ? 0.85 : 0);
    layers.leftover.g.setAttribute('transform', `translate(${ST[9].x} ${ST[9].y - 4})`);
  }

  function renderStations() {
    $$('.station', stage).forEach(g => {
      const i = +g.dataset.i;
      g.classList.toggle('active', i === S.idx);
      g.classList.toggle('visited', S.visited.has(i));
      g.setAttribute('aria-current', i === S.idx ? 'step' : 'false');
    });
    $$('.chip').forEach(c => {
      const i = +c.dataset.i;
      c.classList.toggle('active', i === S.idx);
      c.classList.toggle('visited', S.visited.has(i));
      c.setAttribute('aria-selected', i === S.idx);
    });
    $('#counter').textContent = `${S.idx} / 14`;
    $('#prev').disabled = S.idx <= 1;
    $('#next').textContent = S.idx === 0 ? 'Start →' : S.idx === 14 ? 'Again, -resume →' : 'Next →';
  }

  let detailIdx = -1;
  function flags() {
    const j = job();
    return {
      rerun: S.rerun,
      running: !!S.sim,
      stopped: !!j && j.state === 'stopped',
      retryable: S.needsResume && !!j && j.state !== 'stopped' && !S.sim,
      done: S.rerun || collected(),
      loaded: S.loaded,
    };
  }

  function binds() {
    const j = job(), f = flags();
    const key = S.params45 ? KEYS.cycles45 : KEYS.plain;
    return {
      key: `<mark>${key.slice(0, 16)}</mark>${key.slice(16)}`,
      keynote: (S.params45
        ? 'A different key, so a different job: the ledger would queue it, and the plate would run again. Params are part of what a job is.'
        : 'Same protocol, plate, wells and params: the same key, on every retry, <code>-resume</code> or fresh launch.')
        + (keysLive ? ' <span class="hint">(Computed in your browser just now.)</span>' : ''),
      resubmit: S.resubmitMsg || '',
      deck: S.loaded ? (S.unloaded ? 'Q01 was unloaded: qpcr-1 is free for the next plate.' : 'Loaded and confirmed: qpcr-1 is ready, and Q01\'s job starts there.') : '',
      robot: robotText(j, f),
      calls: callsText(),
    };
  }

  function robotText(j, f) {
    if (S.rerun) return '';
    if (!j || j.state === 'queued') return 'Q01 waits for qpcr-1.';
    if (S.sim) {
      if (S.sim.phase === 'preflight') return 'Pre-flight: the lid closes, the block heats. <b>Still safe to stop.</b>';
      const at = S.sim.phase === 'rt' ? 'Reverse transcription at 50 °C' : `Cycle ${Math.max(1, Math.floor(S.cycle))} of 40`;
      return `${at} · <b>past the stop boundary</b>: this plate runs to the end${S.lateAbort ? ', whatever the pipeline does' : ''}.`;
    }
    const n = j.attempt;
    switch (j.state) {
      case 'failed_preflight':
        return `Stopped safely before the program started: the plate is as it was loaded. The job counts as a pre-flight failure (reason <code>safe_stop</code>). labctl exited 143 and the run ended. <code>-resume</code> submits the same key and gets attempt ${n + 1}.`;
      case 'stopped':
        return `E-stopped at cycle ${Math.floor(S.cycle)}. The job is <code>stopped</code>, never retried by itself: labctl exited 1, the run finished what other robots had in hand, and qpcr-1 waits for a person. Check the deck, then:`;
      case 'voided':
        return `Voided: the material is spoiled. Prepare Q01 again, load it, and rerun with <code>-resume</code>: the job runs again as attempt ${n + 1}.`;
      case 'done':
        if (S.needsResume && S.lateAbort) return 'Too late to stop safely: Q01 finished on qpcr-1, and the ledger keeps the result. labctl exited 143, so the run ended; <code>-resume</code> collects the result without running anything.';
        if (S.needsResume) return 'Resolved as done: the binding reads the result off the deck. Rerun with <code>-resume</code> to collect it.';
        return `Done${n > 1 ? `, on attempt ${n}` : ''}: 40 cycles, a Ct for every well that crossed the threshold. qpcr-1 holds the plate until someone unloads it.`;
    }
    return '';
  }

  function callsText() {
    const s = WELLS.filter(w => w.type === 'sample');
    const det = s.filter(w => w.ct != null && w.ct < 35).length;
    if (!(S.rerun || (job() && job().state === 'done'))) return 'No Ct values yet: Q01 hasn\'t run.';
    return `Q01: controls pass (NTC and blank undetermined, positive control at Ct 25.0) · <b>${det} detected</b>, ${s.length - det} not detected.`;
  }

  function ledgerTable() {
    if (!S.attempts.length) return '<p class="hint">No jobs for this key yet.</p>';
    return `<table><thead><tr><th>Attempt</th><th>Job</th><th>State</th><th>Device</th><th>Reason</th></tr></thead><tbody>${
      S.attempts.map(a => `<tr><td>${a.attempt}</td><td><code>${a.job.slice(0, 8)}…</code></td><td><span class="status ${a.state}">${a.state}</span></td><td>${a.device ? `<code>${a.device}</code>` : '–'}</td><td>${esc(a.reason) || '–'}</td></tr>`).join('')
    }</tbody></table>`;
  }

  function reportTable() {
    if (!S.attempts.length) return '<p class="hint">Nothing to report yet.</p>';
    return `<table><thead><tr><th>plate</th><th>protocol</th><th>attempt</th><th>state</th><th>device_id</th><th>reason</th><th>tasks</th></tr></thead><tbody>${
      S.attempts.map(a => `<tr><td>Q01</td><td>qpcr</td><td>${a.attempt}</td><td><span class="status ${a.state}">${a.state}</span></td><td>${a.device || ''}</td><td>${esc(a.reason)}</td><td><code>lid://${a.task.slice(0, 10)}…</code></td></tr>`).join('')
    }</tbody></table>`;
  }

  function deckTable() {
    const rows = POOL.map(b => {
      const st = b.me ? deviceStatus() : b.status;
      const plate = b.me ? (st === 'empty' ? '' : 'Q01') : b.plate;
      return `<tr><td><code>${b.id}</code></td><td><span class="status ${st}">${st}</span></td><td>${plate}</td></tr>`;
    }).join('');
    const queue = !S.rerun && job() && job().state === 'queued' && !S.loaded
      ? '<tr><td>Q01</td><td><code>qpcr</code></td><td>plate Q01 isn\'t on any deck: load it on qpcr-1</td></tr>'
      : '<tr><td colspan="3" class="hint">Nothing waiting for a person.</td></tr>';
    return `<table><thead><tr><th>Robot</th><th>Status</th><th>Plate</th></tr></thead><tbody>${rows}</tbody></table>
      <table><thead><tr><th>Queue</th><th>Capability</th><th>Waiting for</th></tr></thead><tbody>${queue}</tbody></table>`;
  }

  function highlight(pre) {
    // Comments in pipeline code: Groovy `//` and shell `#` lines, unless already marked up.
    if (pre.querySelector('span')) return;
    pre.innerHTML = pre.innerHTML.replace(/(^|\s)(\/\/[^\n]*)/g, '$1<span class="c">$2</span>');
  }

  function renderDetail() {
    const box = $('#detail');
    if (detailIdx !== S.idx) {
      detailIdx = S.idx;
      box.innerHTML = '';
      box.appendChild($(`#st-${S.idx}`).content.cloneNode(true));
      box.classList.toggle('lab', S.idx >= 5 && S.idx <= 10);
      $$('pre code', box).forEach(highlight);
      const cb = $('[data-action="toggle-params"]', box);
      if (cb) cb.checked = S.params45;
      // Two columns: bring the new story into view if the reader had scrolled past its top.
      if (matchMedia('(min-width: 1180px)').matches && box.getBoundingClientRect().top < 0) {
        window.scrollTo({ top: window.scrollY + $('#tour').getBoundingClientRect().top - 16, behavior: REDUCED ? 'auto' : 'smooth' });
      }
    }
    const f = flags(), b = binds();
    $$('[data-when]', box).forEach(e => {
      e.hidden = !e.dataset.when.split(' ').every(t => t[0] === '!' ? !f[t.slice(1)] : f[t]);
    });
    $$('[data-bind]', box).forEach(e => { const v = b[e.dataset.bind]; if (e.innerHTML !== v) e.innerHTML = v; });
    $$('[data-slot]', box).forEach(e => {
      const v = { ledger: ledgerTable, report: reportTable, deck: deckTable }[e.dataset.slot]();
      if (e.innerHTML !== v) e.innerHTML = v;
    });
  }

  function renderFacts() {
    const j = job(), i = S.idx;
    const pend = (cond, v) => cond ? v : null;
    const facts = [
      ['Station', i ? `${i} · ${ST[i - 1].name}` : 'waiting at the start'],
      ['Task', pend(i >= 3 || S.rerun, `RT_QPCR:QPCR (Q01) [${nfHash(taskHash(S.attempts.length || 1))}]`)],
      ['Capability', pend(i >= 2 || S.rerun, 'qpcr')],
      ['Key', pend(i >= 4 || S.rerun, `${(S.params45 && i === 4 ? KEYS.cycles45 : KEYS.plain).slice(0, 16)}…`)],
      ['Job', pend(j, j && `${j.job.slice(0, 8)}… · attempt ${j.attempt}`)],
      ['State', pend(j, j && (S.sim ? 'running' : j.state))],
      ['Device', pend(S.loaded || S.rerun, j && j.device ? j.device : 'qpcr-1 (loaded)')],
      ['labctl exit', pend(S.exit != null, S.exit)],
      ['Reason', pend(j && j.reason, j && j.reason)],
      ['Calls', pend((i >= 12 || S.rerun) && (S.rerun || collected()), (() => { const s = WELLS.filter(w => w.type === 'sample'); const d = s.filter(w => w.ct != null && w.ct < 35).length; return `${d} detected · ${s.length - d} not`; })())],
    ];
    $('#facts').innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd class="${v == null ? 'pending' : ''}">${v == null ? '…' : esc(v)}</dd>`).join('');
  }

  function render() {
    renderStations();
    renderTerminal();
    renderBay();
    placePlate();
    paintWells(layers.plate.wells, 0.6);
    paintWells(layers.leftover.wells, 0.6);
    paintWells(bigWells, 2);
    renderDetail();
    renderFacts();
  }

  // ---------------------------------------------------------------- the run
  function submit() {
    const n = S.attempts.length + 1;
    S.attempts.push({ attempt: n, job: jobId(n), task: taskHash(n), state: 'queued', reason: '', device: null });
  }

  const T = { rt: 1800, cycles: 3000, end: 9000 };
  function startRun() {
    const j = job();
    j.state = 'running';
    j.device = 'qpcr-1';
    S.cycle = 0;
    S.exit = null;
    S.sim = { t0: performance.now(), phase: 'preflight' };
    const tick = now => {
      if (!S.sim) return;
      const t = now - S.sim.t0;
      const phase = t < T.rt ? 'preflight' : t < T.cycles ? 'rt' : 'cycles';
      const changed = phase !== S.sim.phase;
      S.sim.phase = phase;
      S.cycle = phase === 'cycles' ? Math.min(40, (t - T.cycles) / (T.end - T.cycles) * 40) : 0;
      if (t >= T.end) return finishRun();
      if (changed) render();
      else { renderBay(); renderTerminal(); paintWells(layers.plate.wells, 0.6); paintWells(bigWells, 2); renderDetail(); }
      S.sim.raf = requestAnimationFrame(tick);
    };
    S.sim.raf = requestAnimationFrame(tick);
    render();
  }
  function stopSim() {
    if (S.sim) cancelAnimationFrame(S.sim.raf);
    S.sim = null;
  }
  function finishRun() {
    stopSim();
    const j = job();
    j.state = 'done';
    j.device = 'qpcr-1';
    S.cycle = 40;
    S.exit = S.lateAbort ? 143 : 0;
    S.needsResume = S.lateAbort;
    render();
    if (S.playing) schedule(2600);
  }

  const ACTIONS = {
    ctrlc() {
      if (!S.sim) return;
      if (S.sim.phase === 'preflight') {
        stopSim();
        Object.assign(job(), { state: 'failed_preflight', reason: 'safe_stop' });
        Object.assign(S, { exit: 143, needsResume: true, cycle: 0 });
        toast('Ctrl-C before the stop boundary: labctl sent safe_stop, and nothing happened to the plate.');
      } else {
        S.lateAbort = true;
        toast('Too late to stop safely: cycling has started, so qpcr-1 runs Q01 to the end. The ledger keeps the result.');
      }
      stopPlay();
      render();
    },
    estop() {
      if (!S.sim) return;
      stopSim();
      Object.assign(job(), { state: 'stopped', reason: 'e-stop pressed by ana' });
      Object.assign(S, { exit: 1, needsResume: true });
      toast('E-stop: qpcr-1 stops now, past the boundary too. A person decides what happened.');
      stopPlay();
      render();
    },
    'resolve-done'() {
      if (!job() || job().state !== 'stopped') return;
      Object.assign(job(), { state: 'done', reason: 'resolved as done by ana; was stopped: e-stop pressed by ana' });
      S.cycle = 40;
      render();
    },
    'resolve-void'() {
      if (!job() || job().state !== 'stopped') return;
      Object.assign(job(), { state: 'voided', reason: 'voided by ana; was stopped: e-stop pressed by ana' });
      render();
    },
    resume() {
      if (S.sim || !S.needsResume || job().state === 'stopped') return;
      S.runs++;
      S.needsResume = false;
      S.lateAbort = false;
      const j = job();
      if (j.state === 'done') {
        S.exit = 0;
        toast('-resume: same key, so the ledger returns the done job. labctl writes its outputs; nothing runs again.');
        render();
        return;
      }
      if (j.state === 'voided') toast('Q01 prepared again and loaded on qpcr-1. -resume: a voided key gets a new attempt.');
      else toast('-resume: every earlier attempt failed before anything moved, so the key gets a new attempt.');
      submit();
      startRun();
    },
    resubmit() {
      const j = job();
      ghost();
      if (S.rerun || (j && !['failed_preflight', 'voided'].includes(j.state))) {
        S.resubmitMsg = `<code>POST /jobs</code> → <b>200</b>: the key's existing job <code>${j.job.slice(0, 8)}…</code> (${S.sim ? 'running' : j.state}, attempt ${j.attempt}). Nothing new is queued.`;
      } else {
        submit();
        S.resubmitMsg = `<code>POST /jobs</code> → <b>201</b>: every earlier attempt failed before anything moved or was voided, so this is attempt ${job().attempt}.`;
      }
      render();
    },
    load() {
      if (S.loaded) return;
      S.loaded = true;
      render();
    },
    'toggle-params'(e) {
      S.params45 = e.target.checked;
      render();
    },
    next() { next(); },
  };

  function ghost() {
    if (REDUCED) return;
    const a = ST[3], b = ST[5], g = layers.ghost.g;
    paintWells(layers.ghost.wells, 0.6);
    const t0 = performance.now();
    const step = now => {
      const t = Math.min(1, (now - t0) / 700);
      const x = t < 0.85 ? a.x + (b.x - 30 - a.x) * (t / 0.85) : b.x - 30 - 30 * ((t - 0.85) / 0.15);
      g.setAttribute('transform', `translate(${x} ${a.y - 4})`);
      g.setAttribute('opacity', t < 0.85 ? 0.6 : 0.6 * (1 - (t - 0.85) / 0.15));
      if (t < 1) requestAnimationFrame(step);
      else g.setAttribute('opacity', 0);
    };
    requestAnimationFrame(step);
  }

  // ---------------------------------------------------------------- moving between stations
  let anim = null;
  function effects(i) {
    if (S.rerun) return;
    if (i >= 6 && !S.attempts.length) submit();
    if (i >= 9 && !S.loaded) {
      S.loaded = true;
      toast('A person loaded Q01 on qpcr-1 and confirmed the deck.');
    }
    const j = job();
    if (i > 10 && j && (j.state === 'queued' || S.sim)) {
      if (!S.sim) { j.state = 'running'; j.device = 'qpcr-1'; }
      finishRun();
      toast('Fast-forward: qpcr-1 ran Q01 to the end.');
    }
    if (i >= 13 && collected()) S.unloaded = true;
  }

  function arrive(i) {
    S.idx = i;
    if (i) S.visited.add(i);
    if (i) history.replaceState(null, '', `#station-${i}`);
    effects(i);
    const j = job();
    if (i === 10 && !S.rerun && j && j.state === 'queued' && S.loaded) startRun();
    render();
    if (S.playing) schedule(i === 10 && S.sim ? null : 8000);
  }

  function animateTo(target, done) {
    if (anim) cancelAnimationFrame(anim.raf);
    const from = S.d, dist = Math.abs(target - from);
    if (REDUCED || dist < 1) { S.d = target; anim = null; stage.classList.remove('moving'); done(); return; }
    const dur = Math.max(450, Math.min(2200, dist * 1.6));
    const t0 = performance.now();
    stage.classList.add('moving');
    const step = now => {
      const t = Math.min(1, (now - t0) / dur);
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      S.d = from + (target - from) * e;
      placePlate();
      if (t < 1) anim.raf = requestAnimationFrame(step);
      else { anim = null; stage.classList.remove('moving'); done(); }
    };
    anim = { raf: requestAnimationFrame(step) };
  }

  function go(i) {
    if (i < 1 || i > 14) return;
    clearTimer();
    animateTo(stationD(i), () => arrive(i));
  }

  function wrap() {
    // Lineage → back up the return line → the pipeline, as a -resume of the same session.
    clearTimer();
    stopSim();
    const a = ST[13], b = ST[0], g = layers.plate.g;
    const finish = () => {
      Object.assign(S, { rerun: true, unloaded: true, runs: S.runs + 1, d: 0, exit: 0, needsResume: false, resubmitMsg: '' });
      toast('Same session, -resume. Watch what doesn\'t happen.');
      arrive(1);
    };
    if (REDUCED) return finish();
    const t0 = performance.now();
    stage.classList.add('moving');
    const step = now => {
      const t = Math.min(1, (now - t0) / 1100);
      const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
      g.setAttribute('transform', `translate(${a.x} ${a.y + (b.y - a.y) * e - 4})`);
      if (t < 1) requestAnimationFrame(step);
      else { stage.classList.remove('moving'); finish(); }
    };
    requestAnimationFrame(step);
  }

  function next() {
    if (S.idx === 14) return wrap();
    if (S.idx === 8 && !S.loaded && !S.rerun) {
      S.loaded = true;
      toast('A person loaded Q01 on qpcr-1 and confirmed the deck.');
    }
    if (S.idx === 0) { arrive(1); return; }
    go(S.idx + 1);
  }
  function prev() { if (S.idx > 1) go(S.idx - 1); }

  // ---------------------------------------------------------------- autoplay
  let timer = null, timerT0 = 0, timerDur = 0, progressRaf = null;
  function clearTimer() {
    clearTimeout(timer);
    timer = null;
    cancelAnimationFrame(progressRaf);
    $('#play').style.setProperty('--p', '0%');
  }
  function schedule(ms) {
    clearTimer();
    if (!S.playing || ms == null) return;
    if (S.idx === 14) { stopPlay(); return; }
    timerT0 = performance.now();
    timerDur = ms;
    timer = setTimeout(() => { timer = null; next(); }, ms);
    const bar = () => {
      const p = Math.min(100, (performance.now() - timerT0) / timerDur * 100);
      $('#play').style.setProperty('--p', `${p}%`);
      if (timer) progressRaf = requestAnimationFrame(bar);
    };
    progressRaf = requestAnimationFrame(bar);
  }
  function startPlay() {
    S.playing = true;
    $('#play').textContent = '❚❚ Pause';
    $('#play').setAttribute('aria-pressed', 'true');
    if (S.idx === 14) wrap();
    else next();
  }
  function stopPlay() {
    S.playing = false;
    clearTimer();
    $('#play').textContent = '▶ Play tour';
    $('#play').setAttribute('aria-pressed', 'false');
  }

  function reset() {
    stopPlay();
    stopSim();
    if (anim) cancelAnimationFrame(anim.raf);
    anim = null;
    stage.classList.remove('moving');
    S = fresh();
    detailIdx = -1;
    toast('A new run and a fresh ledger.');
    render();
  }

  let toastTimer = null;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 4200);
  }

  // ---------------------------------------------------------------- wiring
  function init() {
    drawStage();
    drawBigPlate();
    $('#chips').innerHTML = ST.map((s, k) => `<button class="chip ${s.zone === 'nf' ? 'nf' : 'lab'}" data-i="${k + 1}"><b>${k + 1}</b>${s.name}</button>`).join('');
    $$('.chip').forEach(c => c.addEventListener('click', () => { stopPlay(); go(+c.dataset.i); }));
    $('#next').addEventListener('click', () => { stopPlay(); next(); });
    $('#prev').addEventListener('click', () => { stopPlay(); prev(); });
    $('#play').addEventListener('click', () => (S.playing ? stopPlay() : startPlay()));
    $('#reset').addEventListener('click', reset);
    $('#detail').addEventListener('click', e => {
      const a = e.target.closest('[data-action]');
      if (a && a.tagName === 'BUTTON' && ACTIONS[a.dataset.action]) ACTIONS[a.dataset.action](e);
    });
    $('#detail').addEventListener('change', e => {
      const a = e.target.closest('[data-action]');
      if (a && ACTIONS[a.dataset.action]) ACTIONS[a.dataset.action](e);
    });
    stage.addEventListener('click', e => { if (e.target.closest('.station')) stopPlay(); });
    document.addEventListener('keydown', e => {
      if (e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === 'ArrowRight') { stopPlay(); next(); }
      else if (e.key === 'ArrowLeft') { stopPlay(); prev(); }
      else if (e.key === ' ' && !e.target.closest('button, a, [role="button"]')) { e.preventDefault(); S.playing ? stopPlay() : startPlay(); }
      else if (e.key === 'r' || e.key === 'R') reset();
    });
    render();
    const deep = /^#station-(\d+)$/.exec(location.hash);
    if (deep && +deep[1] >= 1 && +deep[1] <= 14) go(+deep[1]);
  }
  init();
})();
