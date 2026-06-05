const SVGNS = "http://www.w3.org/2000/svg";
const fmtUSD = (n, s = true) => {
  if (n == null || isNaN(n)) return "—";
  const sign = n < 0 ? "-" : s && n > 0 ? "+" : "";
  return sign + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};
const pct = (n, d = 1) => (n == null ? "—" : (n * 100).toFixed(d) + "%");
const signClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
const el = (tag, attrs = {}, html) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (html != null) e.textContent = html;
  return e;
};

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(path + " " + r.status);
  return r.json();
}

/* ---------------- Summary plates ---------------- */
function renderPlates(s) {
  const plates = [
    ["Equity", fmtUSD(s.equity, false), `start ${fmtUSD(s.starting_cash, false)}`, ""],
    ["Total P&L", fmtUSD(s.total_pnl), pct(s.roi, 2) + " ROI", signClass(s.total_pnl)],
    ["Realized", fmtUSD(s.realized), `${s.settled} settled`, signClass(s.realized)],
    ["Unrealized", fmtUSD(s.unrealized), `${s.open_positions} open`, signClass(s.unrealized)],
    ["Cash", fmtUSD(s.cash, false), "available", ""],
    ["Win Rate", s.win_rate == null ? "—" : pct(s.win_rate, 0), `${s.wins}/${s.settled}`, ""],
    ["Brier", s.brier == null ? "—" : s.brier.toFixed(3), "lower = sharper", ""],
    ["Positions", String(s.open_positions), "live marks", ""],
  ];
  const c = document.getElementById("plates");
  c.innerHTML = "";
  for (const [k, v, sub, cls] of plates) {
    const d = document.createElement("div");
    d.className = "plate";
    d.innerHTML = `<span class="k">${k}</span><span class="v ${cls}">${v}</span><span class="sub">${sub}</span>`;
    c.appendChild(d);
  }
  document.getElementById("brier-foot").textContent =
    s.brier == null ? "Brier score pending first settlements" : `Brier ${s.brier.toFixed(3)}`;
}

/* ---------------- Equity line chart ---------------- */
function drawEquity(series, start) {
  const svg = document.getElementById("equity-chart");
  svg.innerHTML = "";
  const W = svg.clientWidth, H = svg.clientHeight;
  const m = { t: 14, r: 14, b: 24, l: 56 };
  if (series.length < 2) {
    svg.appendChild(el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "axis-label" },
      "Awaiting equity history — accrues each tick"));
    return;
  }
  const xs = series.map((d) => d.ts);
  const ys = series.map((d) => d.equity).concat([start]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.15 || 5; y0 -= pad; y1 += pad;
  const px = (t) => m.l + (W - m.l - m.r) * (t - x0) / (x1 - x0 || 1);
  const py = (v) => m.t + (H - m.t - m.b) * (1 - (v - y0) / (y1 - y0 || 1));

  // gridlines + y labels
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4, y = py(v);
    svg.appendChild(el("line", { x1: m.l, y1: y, x2: W - m.r, y2: y, class: "grid-line" }));
    svg.appendChild(el("text", { x: m.l - 8, y: y + 3, "text-anchor": "end", class: "axis-label" },
      "$" + v.toFixed(0)));
  }
  // baseline (starting cash)
  svg.appendChild(el("line", { x1: m.l, y1: py(start), x2: W - m.r, y2: py(start), class: "base-line" }));

  const pts = series.map((d) => [px(d.ts), py(d.equity)]);
  // area
  let area = `M${pts[0][0]},${py(start)} `;
  pts.forEach((p) => (area += `L${p[0]},${p[1]} `));
  area += `L${pts[pts.length - 1][0]},${py(start)} Z`;
  svg.appendChild(el("path", { d: area, class: "eq-area" }));
  svg.appendChild(el("path", { d: "M" + pts.map((p) => p.join(",")).join(" L"), class: "eq-line" }));
  // last point marker
  const last = pts[pts.length - 1];
  svg.appendChild(el("circle", { cx: last[0], cy: last[1], r: 3, fill: "var(--ink)" }));

  // x labels (first / last time)
  const fmtT = (t) => new Date(t * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric" });
  svg.appendChild(el("text", { x: m.l, y: H - 6, class: "axis-label" }, fmtT(x0)));
  svg.appendChild(el("text", { x: W - m.r, y: H - 6, "text-anchor": "end", class: "axis-label" }, fmtT(x1)));

  // ---- interactivity: crosshair + tooltip tracking the nearest point ----
  const cross = el("line", { class: "crosshair", y1: m.t, y2: H - m.b, x1: 0, x2: 0,
    style: "display:none" });
  const dot = el("circle", { r: 4, class: "crosshair-dot", style: "display:none" });
  svg.appendChild(cross); svg.appendChild(dot);
  const fmtFull = (t) => new Date(t * 1000).toLocaleString("en-US",
    { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const hit = el("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b,
    fill: "transparent", style: "cursor:crosshair" });
  hit.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    // nearest sample by x
    let bi = 0, best = Infinity;
    pts.forEach((p, i) => { const dd = Math.abs(p[0] - mx); if (dd < best) { best = dd; bi = i; } });
    const d = series[bi], p = pts[bi];
    cross.setAttribute("x1", p[0]); cross.setAttribute("x2", p[0]); cross.style.display = "block";
    dot.setAttribute("cx", p[0]); dot.setAttribute("cy", p[1]); dot.style.display = "block";
    const pnl = d.equity - start;
    tip().innerHTML =
      `<b>${fmtUSD(d.equity, false)}</b>` +
      `<span class="row">${fmtFull(d.ts)}</span>` +
      `<span class="row edge ${pnl >= 0 ? "pos" : "neg"}">P&L<em>${fmtUSD(pnl)}</em></span>` +
      (d.realized != null ? `<span class="row"><i class="d" style="background:var(--gain)"></i>Realized<em>${fmtUSD(d.realized)}</em></span>` : "") +
      (d.unrealized != null ? `<span class="row"><i class="d" style="background:var(--brass-soft)"></i>Unrealized<em>${fmtUSD(d.unrealized)}</em></span>` : "");
    const tp = tip(); tp.style.display = "block";
    const tw = tp.offsetWidth, x2 = e.clientX + 16;
    tp.style.left = (x2 + tw > window.innerWidth ? e.clientX - tw - 16 : x2) + "px";
    tp.style.top = (e.clientY + 16) + "px";
  });
  hit.addEventListener("mouseleave", () => {
    cross.style.display = "none"; dot.style.display = "none"; tip().style.display = "none";
  });
  svg.appendChild(hit);
}

/* ---------------- Open positions ---------------- */
function renderPositions(rows) {
  document.getElementById("pos-count").textContent = `${rows.length} held`;
  const tb = document.querySelector("#positions tbody");
  tb.innerHTML = "";
  if (!rows.length) { tb.innerHTML = `<tr><td colspan="8" class="empty">No open positions</td></tr>`; return; }
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="l mkt" title="${r.question}">${shortQ(r.question)}</td>
      <td class="l">${r.city}</td>
      <td><span class="tag ${r.side.toLowerCase()}">${r.side}</span></td>
      <td class="r">${r.entry_price.toFixed(3)}</td>
      <td class="r">${r.mark_price.toFixed(3)}</td>
      <td class="r">${pct(r.model_prob, 0)}</td>
      <td class="r">${(r.edge * 100).toFixed(1)}%</td>
      <td class="r ${signClass(r.pnl)}">${fmtUSD(r.pnl)}</td>`;
    tb.appendChild(tr);
  }
}

/* ---------------- Blotter ---------------- */
function renderBlotter(rows) {
  document.getElementById("blotter-count").textContent = `${rows.length} fills`;
  const tb = document.querySelector("#blotter tbody");
  tb.innerHTML = "";
  if (!rows.length) { tb.innerHTML = `<tr><td colspan="7" class="empty">No fills yet</td></tr>`; return; }
  for (const r of rows.slice(0, 60)) {
    const t = new Date(r.ts * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="l">${t}</td>
      <td class="l mkt" title="${r.question}">${shortQ(r.question)}</td>
      <td><span class="tag ${r.side.toLowerCase()}">${r.side}</span></td>
      <td class="r">${r.entry_price.toFixed(3)}</td>
      <td class="r">${fmtUSD(r.cost, false)}</td>
      <td><span class="tag ${r.status}">${r.status}</span></td>
      <td class="r ${r.status === "settled" ? signClass(r.pnl) : ""}">${r.status === "settled" ? fmtUSD(r.pnl) : "—"}</td>`;
    tb.appendChild(tr);
  }
}

/* ---------------- Daily PnL bars ---------------- */
function drawDaily(data) {
  const svg = document.getElementById("daily-chart");
  svg.innerHTML = "";
  const W = svg.clientWidth, H = svg.clientHeight;
  const m = { t: 14, r: 14, b: 26, l: 50 };
  const rows = data.realized || [];
  if (!rows.length) {
    svg.appendChild(el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "axis-label" },
      "No settled days yet — markets resolve next day"));
    return;
  }
  const vals = rows.map((d) => d.pnl);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = (hi - lo) * 0.15 || 1; lo -= pad; hi += pad;
  const py = (v) => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo));
  const bw = (W - m.l - m.r) / rows.length;
  svg.appendChild(el("line", { x1: m.l, y1: py(0), x2: W - m.r, y2: py(0), class: "grid-line" }));
  rows.forEach((d, i) => {
    const x = m.l + i * bw + bw * 0.18, w = bw * 0.64;
    const y = py(Math.max(0, d.pnl)), h = Math.abs(py(d.pnl) - py(0));
    svg.appendChild(el("rect", { x, y, width: w, height: Math.max(h, 1),
      fill: d.pnl >= 0 ? "var(--gain)" : "var(--loss)", opacity: .85 }));
    svg.appendChild(el("text", { x: x + w / 2, y: H - 8, "text-anchor": "middle", class: "axis-label" },
      d.day.slice(5)));
  });
}

/* ---------------- Forecast vs market grouped bars ---------------- */
function drawForecast(fc) {
  const stats = document.getElementById("fc-stats");
  const svg = document.getElementById("fc-chart");
  svg.innerHTML = "";
  if (fc.error) { stats.innerHTML = `<span class="empty">${fc.error}</span>`; return; }
  stats.innerHTML = `<b>${fc.city}</b> · ${fc.station} · ${fc.date} — ensemble max
    <b>${fc.mean.toFixed(1)}°C</b> ± ${fc.std.toFixed(1)}°`;
  const W = svg.clientWidth, H = svg.clientHeight;
  const m = { t: 12, r: 10, b: 26, l: 30 };
  const b = fc.buckets;
  const hi = Math.max(...b.map((x) => Math.max(x.model, x.market)), 0.1);
  const py = (v) => m.t + (H - m.t - m.b) * (1 - v / hi);
  const gw = (W - m.l - m.r) / b.length;
  for (let i = 0; i <= 2; i++) {
    const v = hi * i / 2, y = py(v);
    svg.appendChild(el("line", { x1: m.l, y1: y, x2: W - m.r, y2: y, class: "grid-line" }));
    svg.appendChild(el("text", { x: m.l - 6, y: y + 3, "text-anchor": "end", class: "axis-label" }, pct(v, 0)));
  }
  const baseY = H - m.b;
  b.forEach((d, i) => {
    const x = m.l + i * gw;
    const bw = gw * 0.34;
    const mkt = el("rect", { x: x + gw * 0.12, y: py(d.market), width: bw,
      height: baseY - py(d.market), fill: "var(--brass-soft)" });
    const mdl = el("rect", { x: x + gw * 0.12 + bw, y: py(d.model), width: bw,
      height: baseY - py(d.model), fill: "var(--blue)" });
    svg.appendChild(mkt);
    svg.appendChild(mdl);
    svg.appendChild(el("text", { x: x + gw / 2, y: H - 9, "text-anchor": "middle", class: "axis-label" }, d.label));

    // transparent full-height hit area for hover (tooltip + column highlight)
    const hit = el("rect", { x, y: m.t, width: gw, height: baseY - m.t,
      fill: "transparent", style: "cursor:crosshair" });
    const edge = d.model - d.market;
    const show = (e) => {
      tip().innerHTML =
        `<b>${d.label}C</b>` +
        `<span class="row"><i class="d model"></i>Model<em>${(d.model * 100).toFixed(1)}%</em></span>` +
        `<span class="row"><i class="d market"></i>Market<em>${(d.market * 100).toFixed(1)}%</em></span>` +
        `<span class="row edge ${edge >= 0 ? "pos" : "neg"}">Edge<em>${edge >= 0 ? "+" : ""}${(edge * 100).toFixed(1)}%</em></span>`;
      const t = tip();
      t.style.display = "block";
      const tw = t.offsetWidth, x2 = e.clientX + 16;
      t.style.left = (x2 + tw > window.innerWidth ? e.clientX - tw - 16 : x2) + "px";
      t.style.top = (e.clientY + 16) + "px";
    };
    hit.addEventListener("mouseenter", () => {
      hit.setAttribute("fill", "rgba(33,29,22,0.05)");
      mkt.setAttribute("opacity", "1"); mdl.setAttribute("opacity", "1");
      mkt.setAttribute("stroke", "var(--ink)"); mdl.setAttribute("stroke", "var(--ink)");
      mkt.setAttribute("stroke-width", "0.6"); mdl.setAttribute("stroke-width", "0.6");
    });
    hit.addEventListener("mousemove", show);
    hit.addEventListener("mouseleave", () => {
      hit.setAttribute("fill", "transparent");
      mkt.removeAttribute("stroke"); mdl.removeAttribute("stroke");
      tip().style.display = "none";
    });
    svg.appendChild(hit);
  });
}

function tip() {
  let t = document.getElementById("chart-tip");
  if (!t) { t = document.createElement("div"); t.id = "chart-tip"; document.body.appendChild(t); }
  return t;
}

/* ---------------- System almanac ---------------- */
function renderAlmanac(s) {
  const chips = document.getElementById("almanac-chips");
  const strat = s.strategies || {};
  const items = [
    [s.dry_run ? "Dry Run" : "LIVE TRADING", s.dry_run ? "live" : "warn", true],
    ["Forecast Edge", "on", strat.forecast_edge],
    ["Nowcast", "on", strat.nowcast],
    ["Corr-Kelly", "on", strat.corr_kelly],
    ["Arb Exec", "on", strat.arb_execute],
    ["LP Quotes", "on", strat.lp_execute],
  ];
  chips.innerHTML = "";
  for (const [label, cls, active] of items) {
    const c = document.createElement("span");
    c.className = "chip" + (active ? " " + cls : "");
    c.textContent = (active && cls === "on" ? "● " : "") + label;
    chips.appendChild(c);
  }
  const k = s.knobs || {};
  document.getElementById("almanac-meta").innerHTML =
    `<b>${s.stations}</b> stations · <b>${s.emos_fitted}</b> EMOS-calibrated · ` +
    `models <b>${(s.models || []).map((m) => m.split("_")[0].toUpperCase()).join(" · ")}</b> · ` +
    `min-edge <b>${pct(k.min_edge, 0)}</b> · <b>${(k.kelly_fraction * 100).toFixed(0)}%</b> Kelly · ` +
    `bankroll <b>${fmtUSD(k.bankroll, false)}</b> · <b>${s.forecasts_logged}</b> forecasts logged`;
}

/* ---------------- Intraday nowcast (Tier 3) ---------------- */
function drawNowcast(nc) {
  const svg = document.getElementById("nc-chart");
  svg.innerHTML = "";
  const floor = document.getElementById("nc-floor");
  const fill = document.getElementById("nc-meter-fill");
  const mlabel = document.getElementById("nc-meter-label");
  document.getElementById("nc-event").textContent = nc.error ? "" :
    `${nc.city} · ${nc.station} · ${nc.date}`;
  if (nc.error) {
    floor.innerHTML = ""; fill.style.width = "0%"; mlabel.textContent = nc.error;
    svg.appendChild(el("text", { x: 10, y: 30, class: "axis-label" }, nc.error));
    return;
  }
  const om = nc.observed_max == null ? "no obs yet" : `<b>${nc.observed_max.toFixed(0)}°C</b>`;
  floor.innerHTML = `observed floor ${om} · ${nc.remaining_hours}h left · ` +
    `now <b>${nc.mean.toFixed(1)}°C</b> ±${nc.std.toFixed(1)}`;
  const lock = (nc.floor_locked || 0) * 100;
  fill.style.width = lock.toFixed(0) + "%";
  mlabel.textContent = `${lock.toFixed(0)}% locked to floor`;

  const W = svg.clientWidth, H = svg.clientHeight;
  const m = { t: 12, r: 10, b: 26, l: 30 };
  const b = nc.buckets;
  const hi = Math.max(...b.map((x) => Math.max(x.now, x.market, x.ens || 0)), 0.1);
  const py = (v) => m.t + (H - m.t - m.b) * (1 - v / hi);
  const gw = (W - m.l - m.r) / b.length;
  const baseY = H - m.b;
  for (let i = 0; i <= 2; i++) {
    const v = hi * i / 2, y = py(v);
    svg.appendChild(el("line", { x1: m.l, y1: y, x2: W - m.r, y2: y, class: "grid-line" }));
    svg.appendChild(el("text", { x: m.l - 6, y: y + 3, "text-anchor": "end", class: "axis-label" }, pct(v, 0)));
  }
  b.forEach((d, i) => {
    const x = m.l + i * gw, bw = gw * 0.34;
    const mkt = el("rect", { x: x + gw * 0.12, y: py(d.market), width: bw,
      height: baseY - py(d.market), fill: "var(--brass-soft)" });
    const now = el("rect", { x: x + gw * 0.12 + bw, y: py(d.now), width: bw,
      height: baseY - py(d.now), fill: "var(--blue)" });
    svg.appendChild(mkt); svg.appendChild(now);
    // ensemble as a thin reference tick
    if (d.ens != null) {
      const ey = py(d.ens), ex = x + gw * 0.12;
      svg.appendChild(el("line", { x1: ex, y1: ey, x2: ex + bw * 2, y2: ey,
        stroke: "var(--rule-strong)", "stroke-width": 1.4, "stroke-dasharray": "2 2" }));
    }
    svg.appendChild(el("text", { x: x + gw / 2, y: H - 9, "text-anchor": "middle", class: "axis-label" }, d.label));

    const hit = el("rect", { x, y: m.t, width: gw, height: baseY - m.t, fill: "transparent", style: "cursor:crosshair" });
    const edge = d.now - d.market;
    hit.addEventListener("mousemove", (e) => {
      tip().innerHTML =
        `<b>${d.label}C</b>` +
        (d.ens != null ? `<span class="row"><i class="d" style="background:var(--rule-strong)"></i>Ensemble<em>${(d.ens * 100).toFixed(1)}%</em></span>` : "") +
        `<span class="row"><i class="d model"></i>Nowcast<em>${(d.now * 100).toFixed(1)}%</em></span>` +
        `<span class="row"><i class="d market"></i>Market<em>${(d.market * 100).toFixed(1)}%</em></span>` +
        `<span class="row edge ${edge >= 0 ? "pos" : "neg"}">Now−Mkt<em>${edge >= 0 ? "+" : ""}${(edge * 100).toFixed(1)}%</em></span>`;
      const t = tip(); t.style.display = "block";
      const tw = t.offsetWidth, x2 = e.clientX + 16;
      t.style.left = (x2 + tw > window.innerWidth ? e.clientX - tw - 16 : x2) + "px";
      t.style.top = (e.clientY + 16) + "px";
    });
    hit.addEventListener("mouseenter", () => { mkt.setAttribute("stroke", "var(--ink)"); now.setAttribute("stroke", "var(--ink)"); mkt.setAttribute("stroke-width", "0.6"); now.setAttribute("stroke-width", "0.6"); });
    hit.addEventListener("mouseleave", () => { mkt.removeAttribute("stroke"); now.removeAttribute("stroke"); tip().style.display = "none"; });
    svg.appendChild(hit);
  });
}

/* ---------------- Capital allocation ---------------- */
function renderAllocation(ex) {
  document.getElementById("alloc-note").textContent =
    `${fmtUSD(ex.deployed, false)} deployed · ${ex.n} lots`;
  // Yes/No split bar
  const side = document.getElementById("alloc-side");
  side.innerHTML = "";
  const no = ex.by_side.No, yes = ex.by_side.Yes;
  const tot = (no.cost + yes.cost) || 1;
  const seg = (cls, label, n, cost) => {
    const w = (cost / tot) * 100;
    if (w <= 0) return;
    const d = document.createElement("div");
    d.className = "seg " + cls; d.style.width = w + "%";
    d.innerHTML = `<span>${label} ${n} · ${fmtUSD(cost, false)}</span>`;
    d.title = `${label}: ${n} lots, ${fmtUSD(cost, false)} (${w.toFixed(0)}%)`;
    side.appendChild(d);
  };
  seg("no", "No", no.n, no.cost);
  seg("yes", "Yes", yes.n, yes.cost);

  // by-city horizontal bars
  const svg = document.getElementById("alloc-chart");
  svg.innerHTML = "";
  const rows = (ex.by_city || []).slice(0, 8);
  if (!rows.length) { svg.appendChild(el("text", { x: 12, y: 30, class: "axis-label" }, "No open positions")); return; }
  const W = svg.clientWidth, H = svg.clientHeight;
  const m = { t: 6, r: 52, b: 6, l: 78 };
  const hi = Math.max(...rows.map((r) => r.cost));
  const bh = (H - m.t - m.b) / rows.length;
  rows.forEach((r, i) => {
    const y = m.t + i * bh + bh * 0.18, h = bh * 0.64;
    const w = (W - m.l - m.r) * (r.cost / hi);
    svg.appendChild(el("text", { x: m.l - 8, y: y + h / 2 + 3, "text-anchor": "end", class: "axis-label" }, r.city));
    svg.appendChild(el("rect", { x: m.l, y, width: Math.max(w, 1), height: h, fill: "var(--blue)", opacity: .82 }));
    svg.appendChild(el("text", { x: m.l + Math.max(w, 1) + 6, y: y + h / 2 + 3, class: "axis-label" }, fmtUSD(r.cost, false)));
  });
}

/* ---------------- Station calibration (Tier 2) ---------------- */
function renderCalibration(rows) {
  const n = rows.filter((r) => r.emos).length;
  document.getElementById("cal-note").textContent = `${n}/${rows.length} EMOS-fitted`;
  const tb = document.querySelector("#calibration tbody");
  tb.innerHTML = "";
  const f = (v, d = 2) => (v == null ? "—" : (+v).toFixed(d));
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="l">${r.station}</td>
      <td class="l">${r.city}</td>
      <td class="r ${r.bias > 0 ? "neg" : r.bias < 0 ? "pos" : ""}">${r.bias >= 0 ? "+" : ""}${f(r.bias)}</td>
      <td class="r">${f(r.a)}</td><td class="r">${f(r.b)}</td>
      <td class="r">${f(r.c)}</td><td class="r">${f(r.d)}</td>
      <td>${r.emos ? '<span class="tag yes">✓</span>' : '<span class="tag settled">—</span>'}</td>`;
    tb.appendChild(tr);
  }
}

/* ---------------- helpers ---------------- */
function shortQ(q) {
  return q.replace("Will the highest temperature in ", "").replace("?", "");
}

let eventsLoaded = false;
async function loadEvents() {
  const sel = document.getElementById("event-select");
  const [evs, pos] = await Promise.all([get("/weatherbot/api/events"), get("/weatherbot/api/positions")]);
  // Show events we hold a position in first, so the panel matches our trades.
  const held = new Set(pos.map((p) => p.event_slug));
  evs.sort((a, b) => (held.has(b.slug) ? 1 : 0) - (held.has(a.slug) ? 1 : 0));
  sel.innerHTML = "";
  for (const e of evs) {
    const o = document.createElement("option");
    o.value = e.slug;
    o.textContent = (held.has(e.slug) ? "● " : "") + (e.title || e.slug);
    sel.appendChild(o);
  }
  // Default to an event we actually hold (a June 5 market), not today's.
  const def = evs.find((e) => held.has(e.slug));
  if (def) sel.value = def.slug;
  eventsLoaded = true;
  sel.addEventListener("change", refreshForecast);
  refreshForecast();
}
async function refreshForecast() {
  const slug = document.getElementById("event-select").value;
  if (!slug) return;
  document.getElementById("fc-stats").textContent = "loading forecast…";
  try { drawForecast(await get("/weatherbot/api/forecast?event=" + encodeURIComponent(slug))); }
  catch (e) { document.getElementById("fc-stats").innerHTML = `<span class="empty">forecast unavailable</span>`; }
  // the nowcast panel tracks the same selected event (full-day vs intraday view)
  document.getElementById("nc-meter-label").textContent = "computing…";
  try { drawNowcast(await get("/weatherbot/api/nowcast?event=" + encodeURIComponent(slug))); }
  catch (e) { drawNowcast({ error: "nowcast unavailable" }); }
}

/* ---------------- main loop ---------------- */
async function refresh() {
  try {
    const [s, eq, pos, fills, daily, status, exposure, cal] = await Promise.all([
      get("/weatherbot/api/summary"), get("/weatherbot/api/equity"), get("/weatherbot/api/positions"),
      get("/weatherbot/api/fills"), get("/weatherbot/api/daily"),
      get("/weatherbot/api/status"), get("/weatherbot/api/exposure"), get("/weatherbot/api/calibration"),
    ]);
    renderPlates(s);
    renderAlmanac(status);
    drawEquity(eq, s.starting_cash);
    renderPositions(pos);
    renderBlotter(fills);
    drawDaily(daily);
    renderAllocation(exposure);
    renderCalibration(cal);

    const st = document.getElementById("status");
    const live = s.is_live;
    st.classList.toggle("live", live);
    document.getElementById("status-text").textContent = live ? "live" : "stale";
    const lu = s.last_update ? new Date(s.last_update * 1000).toLocaleString("en-US",
      { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";
    document.getElementById("equity-note").textContent = `${fmtUSD(s.equity, false)} · updated ${lu}`;
    document.getElementById("dateline").textContent =
      new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
  } catch (e) {
    document.getElementById("status-text").textContent = "offline";
    document.getElementById("status").classList.remove("live");
  }
  if (!eventsLoaded) loadEvents().catch(() => {});
}

refresh();
setInterval(refresh, 20000);
window.addEventListener("resize", () => refresh());
