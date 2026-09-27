/* 车票 · 机票 比价搜索 —— 前端逻辑（无框架、无依赖） */
'use strict';

const $ = (id) => document.getElementById(id);
const state = { taskId: null, options: [], result: null, mode: 'all', pollTimer: null,
                dateMode: 'single', rangeResult: null };

/* ---------------- 工具 ---------------- */
const fmtPrice = (n) => (n == null ? '—' : '¥' + Number(n).toFixed(0));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function todayStr(offset = 0) {
  const d = new Date();
  d.setDate(d.getDate() + offset);
  return d.toISOString().slice(0, 10);
}

function fmtDuration(min) {
  if (!min) return '—';
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h}小时${String(m).padStart(2, '0')}分` : `${m}分`;
}

/* ---------------- 日期快捷 ---------------- */
function initDates() {
  $('date').value = todayStr(14);
  $('date').min = todayStr(0);
  $('date').max = todayStr(59);
  const quick = [[7, '7天后'], [14, '14天后'], [21, '21天后'], [30, '30天后']];
  const box = $('quickDates');
  quick.forEach(([off, label]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.addEventListener('click', () => { $('date').value = todayStr(off); });
    box.appendChild(b);
  });
}

/* ---------------- 车站联想（城市 + 子车站分组） ---------------- */
function attachAutocomplete(inputEl, boxEl) {
  let timer = null, items = [], flat = [], active = -1;

  const close = () => { boxEl.hidden = true; boxEl.innerHTML = ''; active = -1; };

  // 把分组结构摊平成可键盘选择的条目（城市项 + 每个子车站项）
  const flatten = () => {
    flat = [];
    items.forEach((g, gi) => {
      flat.push({ kind: 'city', group: gi, name: g.name, city: g.city, code: g.code });
      (g.stations || []).forEach((s, si) => {
        flat.push({ kind: 'station', group: gi, si, name: s.name, code: s.code, city: g.city });
      });
    });
  };

  const render = () => {
    if (!items.length) {
      boxEl.innerHTML = '<div class="ac-empty">没有匹配的客运车站</div>';
      boxEl.hidden = false;
      return;
    }
    let idx = 0;
    const html = items.map((g) => {
      const cityIdx = idx++;
      const isMulti = (g.stations || []).length > 1;
      const head = `<div class="ac-city${cityIdx === active ? ' active' : ''}" data-i="${cityIdx}">
          <span class="ac-city-name">${esc(g.city)}</span>
          <span class="muted">${isMulti ? g.station_count + ' 个车站' : esc(g.code)}</span>
          ${isMulti ? '<span class="ac-all">选全城 ↗</span>' : ''}
        </div>`;
      const subs = isMulti ? (g.stations || []).map((s) => {
        const i = idx++;
        const tag = s.is_main ? ' · 主站' : (s.confirmed ? '' : ' · 未确认');
        return `<div class="ac-station${i === active ? ' active' : ''}" data-i="${i}">
            <span>${esc(s.name)}</span>
            <span class="muted">${esc(s.code)}${tag}</span>
          </div>`;
      }).join('') : '';
      return `<div class="ac-group">${head}${subs ? `<div class="ac-subs">${subs}</div>` : ''}</div>`;
    }).join('');
    boxEl.innerHTML = html;
    boxEl.hidden = false;
  };

  const choose = (it) => {
    // 选城市 -> 填入城市名（后端会自动覆盖该城全部客运车站）
    // 选具体车站 -> 填入车站名（只查该站）
    inputEl.value = it.kind === 'city' ? it.city : it.name;
    close();
  };

  inputEl.addEventListener('input', () => {
    clearTimeout(timer);
    const q = inputEl.value.trim();
    if (!q) { close(); return; }
    timer = setTimeout(async () => {
      try {
        const r = await fetch('/api/suggest?q=' + encodeURIComponent(q) + '&grouped=1');
        const j = await r.json();
        items = j.items || [];
        flatten();
        active = -1;
        render();
      } catch { close(); }
    }, 180);
  });

  inputEl.addEventListener('keydown', (e) => {
    if (boxEl.hidden) return;
    if (e.key === 'ArrowDown') { active = Math.min(active + 1, flat.length - 1); render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
    else if (e.key === 'Enter' && active >= 0) { choose(flat[active]); e.preventDefault(); }
    else if (e.key === 'Escape') close();
  });

  boxEl.addEventListener('mousedown', (e) => {
    const node = e.target.closest('[data-i]');
    if (node) { choose(flat[Number(node.dataset.i)]); e.preventDefault(); }
  });

  inputEl.addEventListener('blur', () => setTimeout(close, 180));
}

/* ---------------- 搜索 ---------------- */
async function startSearch(e) {
  if (e) e.preventDefault();
  hide('errorCard'); hide('resultArea'); hide('healthCard');
  if (state.dateMode === 'range') return startRange();

  const payload = {
    from: $('fromCity').value.trim(),
    to: $('toCity').value.trim(),
    date: $('date').value,
    include_train: $('optTrain').checked,
    include_flight: $('optFlight').checked,
    allow_transfer: $('optTransfer').checked,
    allow_air_rail: $('optAirRail').checked,
    exclude_no_baggage: $('optNoBag').checked,
    only_direct: $('optDirect').checked,
    budget: $('budget').value ? Number($('budget').value) : null,
    sort: $('sortSel').value,
  };

  if (!payload.from || !payload.to) return showError('请填写出发地与目的地');
  if (payload.from === payload.to) return showError('出发地与目的地不能相同');

  $('searchBtn').disabled = true;
  show('progressCard');
  setProgress(0, '正在提交搜索…');

  try {
    const r = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok) { $('searchBtn').disabled = false; hide('progressCard'); return showError(j.error || '提交失败'); }
    state.taskId = j.task_id;
    poll();
  } catch (err) {
    $('searchBtn').disabled = false; hide('progressCard');
    showError('无法连接本地服务：' + err.message);
  }
}

/* ---------------- 日期区间搜索 ---------------- */
async function startRange() {
  const payload = {
    from: $('fromCity').value.trim(),
    to: $('toCity').value.trim(),
    start: $('date').value,
    end: $('dateEnd').value || $('date').value,
    include_train: $('optTrain').checked && $('optRangeTrain').checked,
    train_days: $('optRangeTrain').checked ? 5 : 0,
  };
  if (!payload.from || !payload.to) return showError('请填写出发地与目的地');
  if (!payload.start || !payload.end) return showError('请选择区间起止日期');

  $('searchBtn').disabled = true;
  show('progressCard');
  setProgress(0, '正在读取日期区间价格…');
  try {
    const r = await fetch('/api/range', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok) { $('searchBtn').disabled = false; hide('progressCard'); return showError(j.error || '提交失败'); }
    state.taskId = j.task_id;
    state.rangeResult = null;
    pollRange();
  } catch (err) {
    $('searchBtn').disabled = false; hide('progressCard');
    showError('无法连接本地服务：' + err.message);
  }
}

function pollRange() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    try {
      const r = await fetch('/api/progress?task_id=' + encodeURIComponent(state.taskId));
      const j = await r.json();
      if (!r.ok) { $('searchBtn').disabled = false; return showError(j.error || '任务丢失'); }
      setProgress(j.progress, j.message);
      if (j.status === 'running') return pollRange();
      const out = await (await fetch('/api/result?task_id=' + encodeURIComponent(state.taskId))).json();
      $('searchBtn').disabled = false;
      hide('progressCard');
      if (out.status === 'error') return showError(out.message || '区间查询失败');
      renderRange(out.result);
    } catch (err) {
      $('searchBtn').disabled = false;
      hide('progressCard');
      showError('区间查询失败：' + err.message);
    }
  }, 900);
}

function renderRange(res) {
  if (!res) return showError('区间查询无结果');
  state.rangeResult = res;
  const q = res.query || {};
  $('rangeTitle').textContent = `${q.from} → ${q.to}  ${q.start} ~ ${q.end} 每日最低价`;
  const days = res.days || [];
  const withPrice = days.filter((d) => d.min);
  const cheap = res.cheapest;
  $('rangeSub').textContent = withPrice.length
    ? `共 ${days.length} 天，其中 ${withPrice.length} 天有报价`
      + (cheap ? `　最低：${cheap.date}（${cheap.weekday}）¥${Number(cheap.min).toFixed(0)}（${cheap.best_mode}）` : '')
      + `　数据来源：${res.provider || res.source || '-'}`
    : '区间内没有可用的每日报价';

  $('rangeGrid').innerHTML = days.map((d) => {
    const cls = ['day-cell'];
    if (cheap && d.date === cheap.date) cls.push('cheap');
    if (!d.min) cls.push('empty');
    return `<div class="${cls.join(' ')}" data-date="${esc(d.date)}">
      <div><span class="d">${esc(d.date.slice(5))}</span><span class="w">${esc(d.weekday || '')}</span></div>
      <div class="p">${d.min ? '¥' + Number(d.min).toFixed(0) : '—'}</div>
      <div class="m">${d.min ? esc(d.best_mode || '') : '无报价'}</div>
      <div class="m">机 ${d.flight_min ? '¥' + Number(d.flight_min).toFixed(0) : '-'} ·
        火 ${d.train_min ? '¥' + Number(d.train_min).toFixed(0) : '-'}</div>
    </div>`;
  }).join('');

  const note = $('rangeNote');
  note.textContent = (res.note ? res.note + '\n' : '')
    + '点击任意日期可对该日做完整比价（含中转与航铁联程）。';
  note.hidden = false;
  show('rangeArea');
  $('rangeArea').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function poll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    try {
      const r = await fetch('/api/progress?task_id=' + encodeURIComponent(state.taskId));
      const j = await r.json();
      if (!r.ok) { $('searchBtn').disabled = false; return showError(j.error || '任务丢失'); }
      setProgress(j.progress, j.message);
      if (j.status === 'running') return poll();
      await collect();
    } catch (err) {
      $('searchBtn').disabled = false;
      showError('进度查询失败：' + err.message);
    }
  }, 900);
}

async function collect() {
  const r = await fetch('/api/result?task_id=' + encodeURIComponent(state.taskId));
  const j = await r.json();
  $('searchBtn').disabled = false;
  hide('progressCard');
  if (j.status === 'error') return showError(j.message || '搜索失败');
  const res = j.result;
  if (!res) return showError('没有返回结果');
  state.result = res;
  state.options = res.options || [];
  renderResult(res);
}

function setProgress(pct, msg) {
  $('progressBar').style.width = (pct || 0) + '%';
  $('progressPct').textContent = (pct || 0) + '%';
  $('progressText').textContent = msg || '处理中…';
}

/* ---------------- 渲染 ---------------- */
function renderResult(res) {
  const q = res.query || {};
  $('summaryTitle').textContent = `${q.from} → ${q.to} · ${q.date} ${q.weekday || ''}`;
  const s = res.summary || {};
  const pruneInfo = (res.pareto && res.pareto_pruned)
    ? `　已按「价格-耗时」前沿剪掉 ${res.pareto_pruned} 个被支配方案`
    : '';
  $('summarySub').textContent =
    `共 ${s.count || 0} 个方案（火车 ${s.train_count || 0} · 飞机 ${s.flight_count || 0} · 中转 ${s.transfer_count || 0}）`
    + pruneInfo
    + `　耗时 ${res.elapsed || 0}s　生成于 ${res.generated_at || ''}`;
  $('bestPrice').textContent = s.price_min != null ? fmtPrice(s.price_min) : '—';
  $('conclusion').textContent = (res.comparison && res.comparison.conclusion) || '';

  // 火车 / 飞机 对比卡
  const rows = (res.comparison && res.comparison.rows) || [];
  $('modeCompare').innerHTML = rows.map((r) => `
    <div class="mode-box">
      <h3>${esc(r.mode)}<span class="muted"> · ${r.count} 个方案</span></h3>
      <div class="big">${fmtPrice(r.min_price)}<span class="muted" style="font-size:12px"> 起</span></div>
      <dl>
        <dt>最低价方案</dt><dd>${esc(r.min_price_title)}</dd>
        <dt>最快</dt><dd>${esc(r.min_duration_text)}（${esc(r.min_duration_title)}）</dd>
        <dt>均价</dt><dd>${fmtPrice(r.avg_price)}</dd>
      </dl>
    </div>`).join('');

  const note = $('flightNote');
  const parts = [];
  if (res.train_note) parts.push('🚄 ' + res.train_note);
  const src = res.train_sources || {};
  const srcKeys = Object.keys(src);
  if (srcKeys.length) {
    parts.push('🚄 火车数据来源：' + srcKeys.map((k) => `${k}（${src[k]} 条）`).join('、')
      + (src['gaotie.com.cn'] ? '　※ gaotie 为公布票价，实时折扣以 12306 为准' : ''));
  }
  if (res.gaotie_note) parts.push('🚄 ' + res.gaotie_note);
  if (res.flight_note) {
    const tag = res.flight_live ? '✈ ' : '⚠ ';
    const who = res.flight_provider ? `【${res.flight_provider}】` : '';
    parts.push(tag + who + res.flight_note);
  }
  const excl = res.excluded_no_baggage || [];
  if (excl.length) {
    parts.push(`🧳 已按「屏蔽无免费托运航班」过滤 ${excl.length} 个航班：`
      + excl.slice(0, 8).map((x) => `${x.flight_no} ${x.airline}（¥${Number(x.price).toFixed(0)}）`).join('、')
      + (excl.length > 8 ? ' 等' : ''));
  }
  if (parts.length) { note.textContent = parts.join('\n'); note.hidden = false; }
  else note.hidden = true;

  renderRows();
  $('footNote').innerHTML =
    `说明：火车票价格来自 12306 实时查询接口，为实时数据；`
    + `机票价格来源标记为 <b>参考价</b> 的为模型估算（去哪儿/携程接口被反爬拦截），`
    + `请点击「核验」到官方页面确认。中转方案的换乘缓冲区间为 30–360 分钟，`
    + `请出行前自行预留充足时间。`;
  show('resultArea');
}

function currentRows() {
  let rows = state.options.slice();
  if (state.mode !== 'all') rows = rows.filter((o) => o.kind === state.mode);
  return rows;
}

function renderRows() {
  const rows = currentRows();
  const minPrice = Math.min(...state.options.map((o) => o.price));
  $('emptyHint').hidden = rows.length > 0;
  $('rows').innerHTML = rows.map((o) => {
    const isBest = o.price === minPrice;
    const cls = ['row'];
    if (isBest) cls.push('best');
    const badge = { train: 'train', flight: 'flight', transfer: 'transfer',
                    airrail: 'airrail' }[o.kind] || 'train';

    const legsHtml = o.legs.length > 1
      ? `<div class="legs">${o.legs.map((l, i) =>
          `${i ? '<b> · 换乘 ' + esc(o.transfer_text || '') + '</b> · ' : ''}` +
          `<b>${esc(l.code)}</b> ${esc(l.depart)}–${esc(l.arrive)} ${esc(l.seat)} ¥${Number(l.price).toFixed(0)}`
        ).join('')}</div>`
      : (o.all_seats && o.all_seats.length > 1
        ? `<div class="seats">${o.all_seats.slice(0, 6).map((s) =>
            `<span class="seat-chip">${esc(s.seat)} <b>¥${Number(s.price).toFixed(0)}</b></span>`).join('')}</div>`
        : '');

    const bits = (o.note_bits || []).map((b) => esc(b)).join(' · ');
    const overnightTag = o.overnight ? ' · ⚠隔夜换乘（需住宿一晚）' : '';
    const sub = o.kind === 'flight'
      ? `<div class="r">${esc(o.route)}${o.discount ? ` · ${o.discount}折` : ''}${bits ? ' · ' + bits : ''}${o.arrive_next_day ? ' · 次日到达' : ''}</div>`
      : `<div class="r">${esc(o.route)}${o.transfers ? (o.kind === 'airrail' ? ' · 空地换乘' : ' · 一次中转') : ''}${overnightTag}</div>`;

    const detail = (o.kind === 'flight' || o.kind === 'airrail')
      ? `<button class="link-btn subtle" data-detail="${esc(o.title)}">详情</button>` : '';

    return `<div class="${cls.join(' ')}">
      <div class="rank">${o.rank || ''}</div>
      <div><span class="badge ${badge}">${esc(o.mode_label)}</span></div>
      <div class="plan">
        <div class="t">${esc(o.title)}</div>
        ${sub}
        ${legsHtml}
      </div>
      <div class="times">${esc(o.depart)}<span class="arrow">→</span>${esc(o.arrive)}</div>
      <div class="dur">${esc(o.duration)}<div class="muted" style="font-size:11.5px">${o.transfers ? '含中转' : '直达'}</div></div>
      <div class="price">
        <span class="num">${fmtPrice(o.price)}</span>
        <span class="sub">${esc(o.seat)}</span>
      </div>
      <div class="src">
        <span class="${o.live ? 'live' : 'est'}">${o.live ? '● 实时' : '○ 参考价'}</span>
        <div class="muted" style="font-size:11px">${esc(o.source_label)}</div>
      </div>
      <div class="actions">
        ${detail}
        ${o.url ? `<a class="link-btn" href="${esc(o.url)}" target="_blank" rel="noopener">核验</a>` : ''}
      </div>
    </div>`;
  }).join('');
}

/* ---------------- 事件 ---------------- */
function show(id) { $(id).hidden = false; }
function hide(id) { $(id).hidden = true; }
function showError(msg) {
  $('errorText').textContent = msg;
  show('errorCard');
  $('errorCard').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function showHealth() {
  const card = $('healthCard');
  if (!card.hidden) { hide('healthCard'); return; }
  $('healthBody').innerHTML = '<p class="muted">检测中…</p>';
  show('healthCard');
  try {
    const r = await fetch('/api/health');
    const h = await r.json();
    const rows = [];
    const st = h.stations || {};
    rows.push(row(st.source === 'fallback' ? 'warn' : 'ok', '车站库',
      `${st.count} 个车站（来源：${st.source}）`));
    const t = h.train_12306 || {};
    rows.push(row('ok', t.name, `${t.note}　已请求 ${t.requests_made} 次`));
    const fl = h.flights || {};
    for (const key of ['qunar', 'ctrip']) {
      const p = fl[key];
      if (!p) continue;
      const dot = p.reachable ? 'warn' : 'bad';
      rows.push(row(dot, key === 'qunar' ? '去哪儿机票' : '携程机票',
        `${p.reachable ? '站点可达' : '不可达'}　实时报价：${p.live_data ? '可用' : '不可用'}　${p.note || p.error || ''}`));
    }
    $('healthBody').innerHTML = rows.join('') +
      `<p class="muted" style="margin-top:10px;font-size:12px">检测时间：${esc(h.time)}</p>`;
  } catch (err) {
    $('healthBody').innerHTML = `<p class="muted">检测失败：${esc(err.message)}</p>`;
  }
}
const row = (dot, name, note) =>
  `<div class="health-row"><div><span class="dot ${dot}"></span><span class="name">${esc(name)}</span></div>
   <div class="note">${esc(note)}</div></div>`;

function bind() {
  $('searchForm').addEventListener('submit', startSearch);
  $('healthBtn').addEventListener('click', showHealth);
  $('swapBtn').addEventListener('click', () => {
    const a = $('fromCity').value; $('fromCity').value = $('toCity').value; $('toCity').value = a;
  });
  $('dateMode').addEventListener('click', (e) => {
    const btn = e.target.closest('.seg-btn');
    if (!btn) return;
    [...$('dateMode').children].forEach((b) => b.classList.toggle('active', b === btn));
    state.dateMode = btn.dataset.mode;
    $('rangeFields').hidden = state.dateMode !== 'range';
    if (state.dateMode === 'range') {
      const d = new Date($('date').value || todayStr(14));
      d.setDate(d.getDate() + 6);
      if (!$('dateEnd').value) $('dateEnd').value = d.toISOString().slice(0, 10);
    }
  });
  $('rangeClose').addEventListener('click', () => hide('rangeArea'));
  $('rangeGrid').addEventListener('click', (e) => {
    const cell = e.target.closest('.day-cell');
    if (!cell || !cell.dataset.date) return;
    $('date').value = cell.dataset.date;
    [...$('dateMode').children].forEach((b) => b.classList.toggle('active', b.dataset.mode === 'single'));
    state.dateMode = 'single';
    $('rangeFields').hidden = true;
    startSearch();
  });
  $('sortSel').addEventListener('change', async () => {
    if (!state.result) return;
    state.options = resort(state.options, $('sortSel').value);
    state.options.forEach((o, i) => { o.rank = i + 1; });
    renderRows();
  });
  $('tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab');
    if (!btn) return;
    [...$('tabs').children].forEach((b) => b.classList.toggle('active', b === btn));
    state.mode = btn.dataset.mode;
    renderRows();
  });
  $('rows').addEventListener('click', (e) => {
    const b = e.target.closest('[data-detail]');
    if (!b) return;
    const o = state.options.find((x) => x.title === b.dataset.detail);
    if (!o) return;
    const lines = [`方案：${o.title}`, `路线：${o.route}`,
      `时刻：${o.depart} - ${o.arrive}（${o.duration}）`,
      `价格：${fmtPrice(o.price)}（${o.bare_fare ? '裸票价，不含保险等可选加价' : o.source_label}）`];
    if (o.discount) lines.push(`折扣：${o.discount} 折`);
    if (o.note_bits && o.note_bits.length) lines.push(`备注：${o.note_bits.join('、')}`);
    if (o.kind === 'airrail') {
      lines.push('构成：');
      o.legs.forEach((l) => lines.push(`   ${l.code} ${l.from_station}→${l.to_station} ${l.depart}-${l.arrive} ${l.seat} ¥${Number(l.price).toFixed(0)}`));
      lines.push(`换乘缓冲：${o.transfer_text}（需自行从机场前往火车站）`);
    }
    lines.push(`数据来源：${o.source_label}`, o.note || '');
    if (o.urls) {
      lines.push('核验链接：');
      Object.entries(o.urls).forEach(([k, v]) => lines.push(`   ${k}: ${v}`));
    }
    alert(lines.filter(Boolean).join('\n'));
  });
}

function resort(rows, sort) {
  const t = (s) => { const [h, m] = String(s || '99:99').split(':').map(Number); return h * 60 + m; };
  const copy = rows.slice();
  if (sort === 'duration') copy.sort((a, b) => a.duration_min - b.duration_min || a.price - b.price);
  else if (sort === 'depart') copy.sort((a, b) => t(a.depart) - t(b.depart) || a.price - b.price);
  else if (sort === 'transfers') copy.sort((a, b) => a.transfers - b.transfers || a.price - b.price);
  else if (sort === 'value') copy.sort((a, b) => a.price * (1 + a.duration_min / 120) - b.price * (1 + b.duration_min / 120));
  else copy.sort((a, b) => a.price - b.price || a.duration_min - b.duration_min);
  return copy;
}

initDates();
attachAutocomplete($('fromCity'), $('acFrom'));
attachAutocomplete($('toCity'), $('acTo'));
bind();
