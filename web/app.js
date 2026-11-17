/* Watchdog dashboard — report data + simulated catalogue history. */
(() => {
  'use strict';

  const $ = (s) => document.querySelector(s);

  const RULES = [
    { rule: 'ozow-jobs', kind: 'job', status: 'ok', fetched: 9, matched: 8, new: 0, alerts: 0, duration_ms: 3922 },
    { rule: 'entersekt-jobs', kind: 'job', status: 'ok', fetched: 3, matched: 3, new: 0, alerts: 0, duration_ms: 297 },
    { rule: 'luno-jobs', kind: 'job', status: 'ok', fetched: 2, matched: 2, new: 0, alerts: 0, duration_ms: 1046 },
    { rule: 'steam-price-watch', kind: 'price', status: 'ok', fetched: 5, matched: 5, new: 0, alerts: 0, duration_ms: 5812 },
  ];

  const PRODUCTS = [
    { name: 'Elden Ring', base: 699, colour: '#38bdf8', seed: 11 },
    { name: 'Hades II', base: 319, colour: '#a78bfa', seed: 29 },
    { name: 'Hollow Knight', base: 189, colour: '#34d399', seed: 47 },
  ];

  const FEED = [
    { kind: 'price', title: 'Elden Ring dropped to R 489.30', meta: 'steam-price-watch · −30% vs 30-day median', time: '04:17' },
    { kind: 'job', title: 'Senior Backend Engineer — Ozow', meta: 'ozow-jobs · Cape Town / hybrid', time: '04:12' },
    { kind: 'job', title: 'Platform Engineer — Entersekt', meta: 'entersekt-jobs · Stellenbosch', time: '03:58' },
    { kind: 'price', title: 'Hades II matched at R 287.10', meta: 'steam-price-watch · all-time low', time: '03:41' },
    { kind: 'job', title: 'Product Designer — Luno', meta: 'luno-jobs · remote (SA)', time: '02:06' },
  ];

  const el = {
    chart: $('#priceChart'),
    legend: $('#legend'),
    moves: $('#priceMoves'),
    rows: $('#ruleRows'),
    feed: $('#feedList'),
    feedEmpty: $('#feedEmpty'),
    toast: $('#toast'),
    slowest: $('#slowest'),
    slowestMeta: $('#slowestMeta'),
    slowestBar: $('#slowestBar'),
    chartHint: $('#chartHint'),
  };

  let windowDays = 30;
  let hidden = new Set();
  let feedFilter = 'all';
  let feedItems = [...FEED];

  /* --------------------------------------------------------------- helpers */
  function rng(seed) {
    let s = seed >>> 0;
    return () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  function buildSeries(product, days) {
    const random = rng(product.seed + days);
    const points = [];
    let value = product.base;
    for (let i = 0; i < days; i += 1) {
      const drift = (random() - 0.52) * 0.018;
      value = Math.max(product.base * 0.55, value * (1 + drift));
      // occasional sale windows
      if (random() > 0.965) value *= 0.72;
      if (random() > 0.9 && value < product.base) value *= 1.08;
      points.push(Math.round(value * 100) / 100);
    }
    points[points.length - 1] = product.base * (product.seed % 3 === 0 ? 0.7 : 0.92);
    return points;
  }

  function money(value) {
    return `R ${value.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')}`;
  }

  function dateLabels(days) {
    const labels = [];
    const now = new Date();
    for (let i = days - 1; i >= 0; i -= 1) {
      const d = new Date(now.getTime() - i * 864e5);
      labels.push(d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }));
    }
    return labels;
  }

  function toast(message) {
    el.toast.textContent = message;
    el.toast.classList.add('is-visible');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.toast.classList.remove('is-visible'), 2200);
  }

  /* ----------------------------------------------------------------- chart */
  function drawChart() {
    const days = windowDays;
    const series = PRODUCTS.map((product) => ({ ...product, values: buildSeries(product, days) }));
    const visible = series.filter((s) => !hidden.has(s.name));
    if (!visible.length) {
      el.chart.innerHTML = '<text x="360" y="140" text-anchor="middle" class="chart-label">Enable a series to plot it</text>';
      return;
    }

    const W = 720, H = 280;
    const pad = { l: 52, r: 14, t: 16, b: 30 };
    const all = visible.flatMap((s) => s.values);
    const rawMin = Math.min(...all);
    const rawMax = Math.max(...all);
    const min = Math.floor((rawMin * 0.92) / 50) * 50;
    const max = Math.ceil((rawMax * 1.06) / 50) * 50;
    const x = (i) => pad.l + (i / (days - 1)) * (W - pad.l - pad.r);
    const y = (v) => pad.t + (1 - (v - min) / (max - min)) * (H - pad.t - pad.b);

    let svg = '';
    // grid + y labels
    for (let g = 0; g <= 4; g += 1) {
      const value = min + ((max - min) * g) / 4;
      const yy = y(value);
      svg += `<line class="chart__grid" x1="${pad.l}" y1="${yy.toFixed(1)}" x2="${W - pad.r}" y2="${yy.toFixed(1)}" opacity="${g === 0 ? 0.9 : 0.45}"/>`;
      svg += `<text class="chart-label" x="${pad.l - 9}" y="${(yy + 3).toFixed(1)}" text-anchor="end">R${Math.round(value)}</text>`;
    }
    // x labels
    const labels = dateLabels(days);
    const step = Math.max(1, Math.floor(days / 6));
    for (let i = 0; i < days; i += step) {
      if (i === 0 || i > days - step) continue;
      svg += `<text class="chart-label" x="${x(i).toFixed(1)}" y="${H - 8}" text-anchor="middle">${labels[i]}</text>`;
    }

    visible.forEach((s) => {
      const line = s.values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
      const area = `${line} L${x(days - 1).toFixed(1)} ${H - pad.b} L${pad.l} ${H - pad.b} Z`;
      if (visible.length === 1) {
        svg += `<defs><linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="${s.colour}" stop-opacity="0.22"/>
          <stop offset="1" stop-color="${s.colour}" stop-opacity="0"/>
        </linearGradient></defs>`;
        svg += `<path d="${area}" fill="url(#fade)"/>`;
      }
      svg += `<path d="${line}" fill="none" stroke="${s.colour}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`;
      const last = s.values.length - 1;
      svg += `<circle cx="${x(last).toFixed(1)}" cy="${y(s.values[last]).toFixed(1)}" r="3.4" fill="${s.colour}" stroke="#0a0f19" stroke-width="2"/>`;
    });

    el.chart.innerHTML = svg;
    el.chartHint.textContent = `${days}-day window · ${visible.length} serie${visible.length === 1 ? '' : 's'}`;
  }

  function renderLegend() {
    el.legend.innerHTML = PRODUCTS.map((p) =>
      `<button type="button" data-series="${p.name}" aria-pressed="${!hidden.has(p.name)}" style="color:${p.colour}">
        <i></i>${p.name}
      </button>`
    ).join('');
  }

  /* ------------------------------------------------------------ price moves */
  function renderMoves() {
    const rows = PRODUCTS.map((product) => {
      const values = buildSeries(product, 30);
      const last = values[values.length - 1];
      const sorted = [...values].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      const delta = ((last - median) / median) * 100;
      return { product, last, median, delta };
    }).sort((a, b) => a.delta - b.delta);

    el.moves.innerHTML = rows.map(({ product, last, median, delta }) => {
      const cls = delta < -1 ? 'delta--up' : delta > 1 ? 'delta--down' : 'delta--flat';
      const arrow = delta < -1 ? '▼' : delta > 1 ? '▲' : '■';
      return `<li class="list__item">
        <span class="dot" style="background:${product.colour};width:8px;height:8px"></span>
        <div class="list__body price-move">
          <div class="price-move__name"><strong>${product.name}</strong><span>median ${money(median)}</span></div>
          <div class="price-move__price">
            <div>${money(last)}</div>
            <span class="delta ${cls}">${arrow} ${Math.abs(delta).toFixed(1)}%</span>
          </div>
        </div>
      </li>`;
    }).join('');
  }

  /* ----------------------------------------------------------------- rules */
  function renderRules() {
    const total = RULES.reduce((n, r) => n + r.duration_ms, 0);
    el.rows.innerHTML = RULES.map((r) => {
      const share = Math.round((r.duration_ms / total) * 100);
      return `<tr>
        <td class="mono">${r.rule}</td>
        <td><span class="rule-status"><span class="dot"></span>${r.status}</span></td>
        <td class="num">${r.fetched}</td>
        <td class="num">${r.matched}</td>
        <td class="num">${r.new}</td>
        <td class="num">${r.alerts}</td>
        <td><div class="bar-cell"><div class="bar"><i style="width:${share}%"></i></div><span>${(r.duration_ms / 1000).toFixed(1)}s</span></div></td>
        <td class="num">${r.errors ?? 0}</td>
      </tr>`;
    }).join('');

    const slowest = [...RULES].sort((a, b) => b.duration_ms - a.duration_ms)[0];
    const share = Math.round((slowest.duration_ms / total) * 100);
    el.slowest.textContent = slowest.rule;
    el.slowestMeta.textContent = `${(slowest.duration_ms / 1000).toFixed(1)} s · ${share}% of the run`;
    el.slowestBar.style.width = `${share}%`;
  }

  /* ------------------------------------------------------------------ feed */
  const ICONS = {
    job: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="7" width="18" height="13" rx="2.5"/><path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7M3 12h18" stroke-linecap="round"/></svg>',
    price: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 16.5 9.5 11l3.5 3.2L20 7.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M15.5 7.5H20v4.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  function renderFeed() {
    const items = feedItems.filter((item) => feedFilter === 'all' || item.kind === feedFilter);
    el.feed.innerHTML = items.map((item, index) => `<li class="feed__item ${item.fresh ? 'feed__item--new' : ''}">
      <span class="feed__icon">${ICONS[item.kind]}</span>
      <div>
        <div class="feed__title">${item.kind === 'job' ? item.title : item.title}</div>
        <div class="feed__meta">${item.meta}</div>
      </div>
      <span class="feed__time">${item.time}</span>
    </li>`).join('');
    el.feedEmpty.classList.toggle('hidden', items.length > 0);
    feedItems.forEach((item) => { delete item.fresh; });
  }

  function simulate() {
    const jobs = [
      { title: 'Backend Engineer — Ozow', meta: 'ozow-jobs · new listing · Cape Town' },
      { title: 'Data Engineer — Luno', meta: 'luno-jobs · new listing · remote (SA)' },
      { title: 'Mobile Developer — Entersekt', meta: 'entersekt-jobs · new listing · hybrid' },
    ];
    const job = jobs[Math.floor(Math.random() * jobs.length)];
    feedItems.unshift({ ...job, kind: 'job', time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }), fresh: true });
    renderFeed();
    toast('New match dispatched to Telegram');
  }

  /* ---------------------------------------------------------------- events */
  document.querySelectorAll('[data-window]').forEach((tab) => {
    tab.addEventListener('click', () => {
      windowDays = Number(tab.dataset.window);
      document.querySelectorAll('[data-window]').forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
      drawChart();
    });
  });

  el.legend.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-series]');
    if (!btn) return;
    const name = btn.dataset.series;
    if (hidden.has(name)) hidden.delete(name); else hidden.add(name);
    renderLegend();
    drawChart();
  });

  document.querySelectorAll('[data-feed]').forEach((tab) => {
    tab.addEventListener('click', () => {
      feedFilter = tab.dataset.feed;
      document.querySelectorAll('[data-feed]').forEach((t) => t.setAttribute('aria-selected', String(t === tab)));
      renderFeed();
    });
  });

  document.querySelector('#simulateBtn').addEventListener('click', simulate);

  /* ------------------------------------------------------------------ boot */
  renderLegend();
  drawChart();
  renderMoves();
  renderRules();
  renderFeed();
})();
