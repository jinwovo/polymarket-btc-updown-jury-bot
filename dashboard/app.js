const state = {
  snapshot: null,
  history: null,
  lastBannerKey: "",
};

function fmtNumber(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  return Number(v).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtPct(v, digits = 3) {
  if (v === null || v === undefined || Number.isNaN(v)) return "--";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

function fmtCountdown(sec) {
  if (sec === null || sec === undefined || Number.isNaN(sec)) return "--:--";
  const s = Math.max(0, Math.floor(sec));
  const m = String(Math.floor(s / 60)).padStart(2, "0");
  const r = String(s % 60).padStart(2, "0");
  return `${m}:${r}`;
}

function ageText(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "n/a";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  const m = Math.floor(seconds / 60);
  return `${m}m ago`;
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = value;
}

function isFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function buildJudgeRow(j) {
  const voteColor = j.vote === "UP"
    ? "text-emerald-300 border-emerald-400/30 bg-emerald-400/10"
    : j.vote === "DOWN"
      ? "text-rose-300 border-rose-400/30 bg-rose-400/10"
      : "text-slate-300 border-slate-400/30 bg-slate-400/10";
  return `
    <div class="rounded-xl border border-white/10 bg-white/[0.03] p-3">
      <div class="flex items-center justify-between gap-2">
        <p class="text-sm font-semibold">${j.name}</p>
        <span class="mono rounded-md border px-2 py-0.5 text-xs ${voteColor}">
          ${j.vote}
        </span>
      </div>
      <p class="mono mt-1 text-xs text-slate-300">conf ${fmtNumber(j.confidence, 3)}</p>
      <p class="mt-1 text-xs text-slate-400">${j.reason}</p>
    </div>
  `;
}

function renderWindows(rows) {
  const body = document.getElementById("windowRows");
  if (!body) return;
  if (!rows || rows.length === 0) {
    body.innerHTML = `<tr><td class="py-3 text-slate-400" colspan="6">No windows yet</td></tr>`;
    return;
  }
  body.innerHTML = rows.map((w) => {
    const start = w.window_start
      ? new Date(w.window_start * 1000).toISOString().replace("T", " ").slice(0, 19)
      : "--";
    const outcome = w.actual_outcome || "PENDING";
    const outcomeClass = outcome === "UP"
      ? "text-emerald-300"
      : outcome === "DOWN"
        ? "text-rose-300"
        : "text-slate-300";
    const move = w.change_pct === null || w.change_pct === undefined
      ? "--"
      : fmtPct(w.change_pct, 4);
    return `
      <tr class="border-b border-white/5">
        <td class="py-2">${start}</td>
        <td class="py-2 ${outcomeClass}">${outcome}</td>
        <td class="py-2">${fmtNumber(w.btc_start_price, 2)}</td>
        <td class="py-2">${fmtNumber(w.btc_end_price, 2)}</td>
        <td class="py-2">${move}</td>
        <td class="max-w-[260px] truncate py-2 text-slate-400">${w.slug || "--"}</td>
      </tr>
    `;
  }).join("");
}

function applyBanner(signal) {
  const banner = document.getElementById("opportunityBanner");
  if (!banner) return;
  banner.classList.remove("signal-up", "signal-down", "signal-wait", "pulse");

  if (signal.actionable && signal.direction === "UP") {
    banner.classList.add("signal-up", "pulse");
    setText("bannerTitle", "BUY UP opportunity detected");
  } else if (signal.actionable && signal.direction === "DOWN") {
    banner.classList.add("signal-down", "pulse");
    setText("bannerTitle", "BUY DOWN opportunity detected");
  } else {
    banner.classList.add("signal-wait");
    setText("bannerTitle", "No actionable setup right now");
  }

  setText("bannerConfidence", fmtNumber(signal.avg_confidence, 3));
}

function updateSnapshotView(data) {
  const collector = data.collector || {};
  const market = data.market || {};
  const signal = data.signal || {};
  const windowData = data.window || {};

  const running = !!collector.running;
  const statusDot = document.getElementById("statusDot");
  if (statusDot) {
    statusDot.classList.remove("bg-amber-400", "bg-emerald-400", "bg-rose-400");
    statusDot.classList.add(running ? "bg-emerald-400" : "bg-amber-400");
  }
  setText("statusText", running ? "Collector Live" : "Data Delayed");
  setText(
    "statusFreshness",
    `tick ${ageText(collector.last_tick_age_sec)} | odds ${ageText(collector.last_odds_age_sec)}`
  );

  setText("signalDirection", signal.action_label || "WAIT");
  setText("signalReason", signal.reason || "No reason");

  setText("btcMove", fmtPct(market.btc_change_pct, 4));
  setText("btcPrice", `BTC ${fmtNumber(market.btc_price, 2)} | Start ${fmtNumber(market.btc_start_price, 2)}`);

  setText("upMid", fmtNumber(market.up_mid, 3));
  setText("downMid", fmtNumber(market.down_mid, 3));

  const spread = (isFiniteNumber(market.up_ask) && isFiniteNumber(market.up_bid))
    ? market.up_ask - market.up_bid
    : null;
  setText("oddsSpread", `UP spread: ${spread === null ? "--" : fmtNumber(spread, 3)}`);

  setText("windowCountdown", fmtCountdown(windowData.seconds_remaining));
  setText("windowSlug", `slug: ${windowData.slug || "--"}`);
  const progress = Math.max(0, Math.min(100, Number(windowData.progress_pct || 0)));
  const progressEl = document.getElementById("windowProgress");
  if (progressEl) progressEl.style.width = `${progress}%`;

  applyBanner(signal);

  const judgeBox = document.getElementById("judgeVotes");
  if (judgeBox) {
    const judges = signal.judges || [];
    judgeBox.innerHTML = judges.length > 0
      ? judges.map(buildJudgeRow).join("")
      : `<p class="text-sm text-slate-400">Waiting for enough data...</p>`;
  }

  const s = data.stats || {};
  setText(
    "statsLine",
    `ticks=${s.ticks ?? "--"} odds=${s.odds ?? "--"} windows=${s.windows ?? "--"} resolved=${s.resolved_windows ?? "--"}`
  );
  renderWindows(data.recent_windows || []);
}

function drawLineChart(canvas, seriesList, options = {}) {
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 800;
  const height = canvas.clientHeight || 170;
  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const pad = { left: 8, right: 8, top: 8, bottom: 12 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  if (plotW <= 0 || plotH <= 0) return;

  ctx.strokeStyle = "rgba(148,163,184,0.18)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i += 1) {
    const y = pad.top + (plotH * i) / 5;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(width - pad.right, y);
    ctx.stroke();
  }

  seriesList.forEach((series) => {
    const values = series.values || [];
    if (values.length < 2) return;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1e-9;

    ctx.beginPath();
    values.forEach((v, i) => {
      const x = pad.left + (plotW * i) / Math.max(values.length - 1, 1);
      const yNorm = (v - min) / span;
      const y = pad.top + plotH - yNorm * plotH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = series.color;
    ctx.lineWidth = 2;
    ctx.stroke();

    const last = values[values.length - 1];
    const lastX = pad.left + plotW;
    const lastY = pad.top + plotH - ((last - min) / span) * plotH;
    ctx.fillStyle = series.color;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
    ctx.fill();
  });
}

function updateHistoryView(data) {
  const btcValues = (data.btc || [])
    .map((p) => Number(p.value))
    .filter((n) => Number.isFinite(n));
  const upValues = (data.up || [])
    .map((p) => Number(p.value))
    .filter((n) => Number.isFinite(n));
  const downValues = (data.down || [])
    .map((p) => Number(p.value))
    .filter((n) => Number.isFinite(n));

  drawLineChart(
    document.getElementById("btcChart"),
    [{ values: btcValues, color: "rgba(34,211,238,0.95)" }]
  );
  drawLineChart(
    document.getElementById("oddsChart"),
    [
      { values: upValues, color: "rgba(16,185,129,0.95)" },
      { values: downValues, color: "rgba(244,63,94,0.95)" },
    ]
  );
}

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function refreshSnapshot() {
  try {
    const data = await fetchJson("/api/snapshot");
    if (!data.ok) throw new Error(data.error || "snapshot unavailable");
    state.snapshot = data;
    updateSnapshotView(data);
  } catch (err) {
    setText("statusText", "Dashboard Error");
    setText("statusFreshness", String(err.message || err));
    const dot = document.getElementById("statusDot");
    if (dot) {
      dot.classList.remove("bg-amber-400", "bg-emerald-400");
      dot.classList.add("bg-rose-400");
    }
  }
}

async function refreshHistory() {
  try {
    const data = await fetchJson("/api/history?minutes=30");
    if (!data.ok) throw new Error(data.error || "history unavailable");
    state.history = data;
    updateHistoryView(data);
  } catch (_) {
    // Keep last chart.
  }
}

function start() {
  refreshSnapshot();
  refreshHistory();
  setInterval(refreshSnapshot, 2000);
  setInterval(refreshHistory, 6000);
  window.addEventListener("resize", () => {
    if (state.history) updateHistoryView(state.history);
  });
}

start();
