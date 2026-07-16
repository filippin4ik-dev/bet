/* Сканер вилок (прематч) — фронтенд без фреймворков.
 * Опрашивает /api/arbs и /api/odds, рисует таблицы с сортировкой по любому
 * столбцу, поиском и фильтрами; подаёт звук при новой вилке выше порога. */

const POLL_INTERVAL_MS = 10_000;

const els = {
  body: document.getElementById("arbs-body"),
  oddsBody: document.getElementById("odds-body"),
  arbsTable: document.getElementById("arbs-table"),
  oddsTable: document.getElementById("odds-table"),
  viewTabs: document.getElementById("view-tabs"),
  search: document.getElementById("search"),
  sportFilter: document.getElementById("sport-filter"),
  bkFilter: document.getElementById("bk-filter"),
  bkFilterLabel: document.getElementById("bk-filter-label"),
  minProfit: document.getElementById("min-profit"),
  bank: document.getElementById("bank"),
  soundOn: document.getElementById("sound-on"),
  soundThreshold: document.getElementById("sound-threshold"),
  testSound: document.getElementById("test-sound"),
  modeBadge: document.getElementById("mode-badge"),
  lastScan: document.getElementById("last-scan"),
  scanningBadge: document.getElementById("scanning-badge"),
  coverage: document.getElementById("coverage"),
  bkCounts: document.getElementById("bk-counts"),
  arbCount: document.getElementById("arb-count"),
  interval: document.getElementById("interval"),
  tableMeta: document.getElementById("table-meta"),
};

let soundAlertProfit = 2.5;
let alertedKeys = new Set(); // вилки, о которых уже «пропищали»

/* ---------- состояние (сохраняется в localStorage) ---------- */

const state = {
  view: "arbs",                 // arbs | odds
  search: "",
  sport: "",
  bookmaker: "",
  sort: {
    arbs: { key: "profit_pct", dir: -1 },
    odds: { key: "start_ts", dir: 1 },
  },
};

function loadState() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem("arb-scanner-ui") || "{}") || {};
  } catch (_) { /* повреждённое хранилище — игнорируем */ }
  Object.assign(state, saved, { sort: { ...state.sort, ...(saved.sort || {}) } });
  els.search.value = state.search;
  const minProfit = localStorage.getItem("arb-scanner-minProfit");
  if (minProfit !== null) els.minProfit.value = minProfit;
  const bank = localStorage.getItem("arb-scanner-bank");
  if (bank !== null) els.bank.value = bank;
}

function saveState() {
  localStorage.setItem("arb-scanner-ui", JSON.stringify(state));
  localStorage.setItem("arb-scanner-minProfit", els.minProfit.value);
  localStorage.setItem("arb-scanner-bank", els.bank.value);
}

/* ---------- данные последнего опроса ---------- */

let lastArbs = [];
let lastOdds = [];

/* ---------- звук (Web Audio, без файлов) ---------- */

let audioCtx = null;

function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const now = audioCtx.currentTime;
    [880, 1320].forEach((freq, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.001, now + i * 0.18);
      gain.gain.exponentialRampToValueAtTime(0.3, now + i * 0.18 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.18 + 0.16);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(now + i * 0.18);
      osc.stop(now + i * 0.18 + 0.18);
    });
  } catch (_) { /* звук недоступен — не критично */ }
}

els.testSound.addEventListener("click", beep);

/* ---------- сортировка / фильтрация ---------- */

function cmp(a, b, key) {
  const va = a[key], vb = b[key];
  if (va == null && vb == null) return 0;
  if (va == null) return 1;   // пустые — в конец
  if (vb == null) return -1;
  if (typeof va === "number" && typeof vb === "number") return va - vb;
  return String(va).localeCompare(String(vb), "ru");
}

// Корневой вид спорта: «Футбол · США. MLS» -> «Футбол»
const rootSport = (s) => String(s).split(" · ")[0].trim();

function applyFilters(rows) {
  const q = state.search.trim().toLowerCase();
  return rows.filter((r) => {
    if (state.sport && rootSport(r.sport) !== state.sport) return false;
    if (state.view === "odds" && state.bookmaker && r.bookmaker !== state.bookmaker) return false;
    if (q) {
      const hay = `${r.match} ${r.sport} ${r.market} ${r.bookmaker || ""} ${r.k1_bookmaker || ""} ${r.k2_bookmaker || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function applySort(rows) {
  const { key, dir } = state.sort[state.view];
  return [...rows].sort((a, b) => dir * cmp(a, b, key));
}

function updateSortIndicators() {
  const table = state.view === "arbs" ? els.arbsTable : els.oddsTable;
  const { key, dir } = state.sort[state.view];
  document.querySelectorAll("th.sortable").forEach((th) => th.classList.remove("sorted-asc", "sorted-desc"));
  table.querySelectorAll("th.sortable").forEach((th) => {
    if (th.dataset.sort === key) th.classList.add(dir === 1 ? "sorted-asc" : "sorted-desc");
  });
}

function refreshFilterOptions() {
  const rows = state.view === "arbs" ? lastArbs : lastOdds;
  // В фильтре — только корневые виды спорта, без лиг и росписей
  const sports = [...new Set(rows.map((r) => rootSport(r.sport)))].sort((a, b) => a.localeCompare(b, "ru"));
  fillSelect(els.sportFilter, sports, state.sport, "Все");
  if (state.view === "odds") {
    const bks = [...new Set(rows.map((r) => r.bookmaker))].sort();
    fillSelect(els.bkFilter, bks, state.bookmaker, "Все");
  }
  els.bkFilterLabel.hidden = state.view !== "odds";
}

function fillSelect(sel, values, current, allLabel) {
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  sel.value = values.includes(current) ? current : "";
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ---------- отрисовка ---------- */

const fmtMoney = (n) => Number(n).toLocaleString("ru-RU") + " ₽";

function startCell(r) {
  if (!r.start_ts && !r.start_time) return '<span class="muted">—</span>';
  let label = r.start_time || "";
  if (r.start_ts) {
    const d = new Date(r.start_ts * 1000);
    const today = new Date();
    const tomorrow = new Date(today.getTime() + 86400000);
    const sameDay = (a, b) => a.toDateString() === b.toDateString();
    const hm = d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
    if (sameDay(d, today)) label = `Сегодня ${hm}`;
    else if (sameDay(d, tomorrow)) label = `Завтра ${hm}`;
    else label = d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) + ` ${hm}`;
  }
  return `<span class="kind prematch">${escapeHtml(label)}</span>`;
}

function renderMeta(shown, total) {
  els.tableMeta.textContent = shown === total
    ? `Показано: ${shown}`
    : `Показано: ${shown} из ${total} (фильтры активны)`;
}

function renderOdds() {
  const rows = applySort(applyFilters(lastOdds));
  renderMeta(rows.length, lastOdds.length);
  if (!rows.length) {
    els.oddsBody.innerHTML =
      '<tr><td colspan="7" class="empty">Ни одного матча под текущие фильтры — ждём окончания обхода БК…</td></tr>';
    return;
  }
  els.oddsBody.innerHTML = rows.map((o) => `<tr>
      <td><span class="bk-name">${escapeHtml(o.bookmaker)}</span></td>
      <td>${startCell(o)}</td>
      <td>${escapeHtml(o.sport)}</td>
      <td>${escapeHtml(o.match)}</td>
      <td>${escapeHtml(o.market)}</td>
      <td><span class="out">${escapeHtml(o.outcome1)}</span> <span class="coef">${o.k1.toFixed(2)}</span></td>
      <td><span class="out">${escapeHtml(o.outcome2)}</span> <span class="coef">${o.k2.toFixed(2)}</span></td>
    </tr>`).join("");
}

function renderBkCounts(bookmakers) {
  const age = (s) => {
    if (s == null) return "";
    if (s < 90) return "только что";
    return `${Math.round(s / 60)} мин назад`;
  };
  const parts = Object.entries(bookmakers || {}).map(([bk, v]) => {
    if (typeof v === "number") return `${bk}: ${v}`;
    return `${bk}: ${v.count} (${age(v.age_sec)})`;
  });
  els.bkCounts.textContent = parts.length ? parts.join(" · ") : "";
}

function renderArbs() {
  const bank = els.bank.value;
  els.arbCount.textContent = `Вилок: ${lastArbs.length}`;
  const rows = applySort(applyFilters(lastArbs));
  renderMeta(rows.length, lastArbs.length);

  if (!rows.length) {
    els.body.innerHTML =
      '<tr><td colspan="10" class="empty">Вилок нет — ждём следующего обновления…</td></tr>';
    return;
  }

  els.body.innerHTML = rows.map((a) => {
    const st = a.stakes[bank] || {};
    const hot = a.profit_pct > soundAlertProfit ? " class=\"hot\"" : "";
    return `<tr${hot}>
      <td>${startCell(a)}</td>
      <td>${escapeHtml(a.sport)}</td>
      <td>${escapeHtml(a.match)}</td>
      <td>${escapeHtml(a.market)}</td>
      <td><span class="out">${escapeHtml(a.outcome1)}</span> <span class="coef">${a.k1_max.toFixed(2)}</span> <span class="bk">${escapeHtml(a.k1_bookmaker)}</span></td>
      <td><span class="out">${escapeHtml(a.outcome2)}</span> <span class="coef">${a.k2_max.toFixed(2)}</span> <span class="bk">${escapeHtml(a.k2_bookmaker)}</span></td>
      <td class="profit">${a.profit_pct.toFixed(2)} %</td>
      <td class="stake">${fmtMoney(st.stake1)} <span class="bk">${escapeHtml(a.outcome1)} · ${escapeHtml(a.k1_bookmaker)}</span></td>
      <td class="stake">${fmtMoney(st.stake2)} <span class="bk">${escapeHtml(a.outcome2)} · ${escapeHtml(a.k2_bookmaker)}</span></td>
      <td class="stake">+${fmtMoney(st.profit)}</td>
    </tr>`;
  }).join("");
}

function rerender() {
  refreshFilterOptions();
  updateSortIndicators();
  if (state.view === "arbs") renderArbs();
  else renderOdds();
}

/* ---------- опрос API ---------- */

async function poll() {
  try {
    const minProfit = parseFloat(els.minProfit.value) || 0;
    const resp = await fetch(`/api/arbs?min_profit=${minProfit}`);
    const data = await resp.json();

    soundAlertProfit = data.sound_alert_profit ?? 2.5;
    els.soundThreshold.textContent = soundAlertProfit;
    els.interval.textContent = data.scan_interval;

    els.modeBadge.textContent = "ПРЕМАТЧ";
    els.modeBadge.className = "badge live";
    els.scanningBadge.hidden = !data.scanning;

    if (data.last_scan) {
      els.lastScan.textContent = "Обновлено: " +
        new Date(data.last_scan * 1000).toLocaleTimeString("ru-RU");
    }

    if (data.events_checked) {
      els.coverage.textContent =
        `В памяти: ${data.events_checked} событий / ${data.quotes_checked} котировок`;
    }
    renderBkCounts(data.bookmakers);

    lastArbs = data.arbs;

    // Звук — только для НОВЫХ вилок с доходностью выше порога
    const hot = data.arbs.filter((a) => a.profit_pct > soundAlertProfit);
    const fresh = hot.filter((a) => !alertedKeys.has(a.match_key));
    if (fresh.length && els.soundOn.checked) beep();
    alertedKeys = new Set(hot.map((a) => a.match_key));
  } catch (err) {
    els.lastScan.textContent = "Ошибка связи с сервером…";
  }

  if (state.view === "odds") {
    try {
      const resp = await fetch("/api/odds");
      const data = await resp.json();
      lastOdds = data.odds;
    } catch (err) { /* статус уже показан выше */ }
  }
  rerender();
}

/* ---------- обработчики ---------- */

els.minProfit.addEventListener("change", () => { saveState(); poll(); });
els.bank.addEventListener("change", () => { saveState(); rerender(); });

let searchTimer = null;
els.search.addEventListener("input", () => {
  state.search = els.search.value;
  saveState();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(rerender, 150);
});

els.sportFilter.addEventListener("change", () => {
  state.sport = els.sportFilter.value;
  saveState();
  rerender();
});

els.bkFilter.addEventListener("change", () => {
  state.bookmaker = els.bkFilter.value;
  saveState();
  rerender();
});

els.viewTabs.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  state.view = btn.dataset.view;
  saveState();
  els.viewTabs.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("active", b === btn));
  els.arbsTable.hidden = state.view !== "arbs";
  els.oddsTable.hidden = state.view !== "odds";
  poll();
});

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    const sort = state.sort[state.view];
    if (sort.key === key) sort.dir = -sort.dir;
    else {
      sort.key = key;
      // числовые — сначала по убыванию, текст/время — по возрастанию
      sort.dir = ["profit_pct", "k1", "k2", "k1_max", "k2_max"].includes(key) ? -1 : 1;
    }
    saveState();
    rerender();
  });
});

/* ---------- запуск ---------- */

loadState();
els.viewTabs.querySelectorAll("button").forEach(
  (b) => b.classList.toggle("active", b.dataset.view === state.view));
els.arbsTable.hidden = state.view !== "arbs";
els.oddsTable.hidden = state.view !== "odds";

poll();
setInterval(poll, POLL_INTERVAL_MS);
