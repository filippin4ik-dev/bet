/* Сканер вилок — фронтенд без фреймворков.
 * Вкладки: вилки (прематч/лайв) и матчи (прематч/лайв). Список матчей
 * сгруппирован по событиям; клик по матчу открывает страницу со ВСЕМИ
 * котировками этого матча по всем БК (каждая БК своим цветом). */

const POLL_INTERVAL_MS = 10_000;      // прематч — раз в 10 с
const LIVE_POLL_INTERVAL_MS = 5_000;  // лайв обновляем чаще

const els = {
  body: document.getElementById("arbs-body"),
  body1x2: document.getElementById("arbs1x2-body"),
  matchesBody: document.getElementById("matches-body"),
  arbsTable: document.getElementById("arbs-table"),
  arbs1x2Table: document.getElementById("arbs1x2-table"),
  matchesTable: document.getElementById("matches-table"),
  matchDetail: document.getElementById("match-detail"),
  detailTitle: document.getElementById("detail-title"),
  detailMeta: document.getElementById("detail-meta"),
  detailBody: document.getElementById("detail-body"),
  backBtn: document.getElementById("back-btn"),
  viewTabs: document.getElementById("view-tabs"),
  search: document.getElementById("search"),
  sportFilter: document.getElementById("sport-filter"),
  bkFilter: document.getElementById("bk-filter"),
  bkFilterLabel: document.getElementById("bk-filter-label"),
  minProfit: document.getElementById("min-profit"),
  minProfitLabel: document.getElementById("min-profit-label"),
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
  betModal: document.getElementById("bet-modal"),
  betTitle: document.getElementById("bet-title"),
  betMeta: document.getElementById("bet-meta"),
  betClose: document.getElementById("bet-close"),
  betBank: document.getElementById("bet-bank"),
  betOut1: document.getElementById("bet-out1"),
  betOut2: document.getElementById("bet-out2"),
  betK1: document.getElementById("bet-k1"),
  betK2: document.getElementById("bet-k2"),
  betBk1: document.getElementById("bet-bk1"),
  betBk2: document.getElementById("bet-bk2"),
  betStake1: document.getElementById("bet-stake1"),
  betStake2: document.getElementById("bet-stake2"),
  betLink1: document.getElementById("bet-link1"),
  betLink2: document.getElementById("bet-link2"),
  betSummary: document.getElementById("bet-summary"),
  betOpenBoth: document.getElementById("bet-open-both"),
  bet3Modal: document.getElementById("bet3-modal"),
  bet3Title: document.getElementById("bet3-title"),
  bet3Meta: document.getElementById("bet3-meta"),
  bet3Close: document.getElementById("bet3-close"),
  bet3Bank: document.getElementById("bet3-bank"),
  bet3K1: document.getElementById("bet3-k1"),
  bet3Kx: document.getElementById("bet3-kx"),
  bet3K2: document.getElementById("bet3-k2"),
  bet3Bk1: document.getElementById("bet3-bk1"),
  bet3Bkx: document.getElementById("bet3-bkx"),
  bet3Bk2: document.getElementById("bet3-bk2"),
  bet3Stake1: document.getElementById("bet3-stake1"),
  bet3Stakex: document.getElementById("bet3-stakex"),
  bet3Stake2: document.getElementById("bet3-stake2"),
  bet3Link1: document.getElementById("bet3-link1"),
  bet3Linkx: document.getElementById("bet3-linkx"),
  bet3Link2: document.getElementById("bet3-link2"),
  bet3Summary: document.getElementById("bet3-summary"),
  bet3OpenAll: document.getElementById("bet3-open-all"),
};

let soundAlertProfit = 2.5;
let alertedKeys = new Set(); // вилки, о которых уже «пропищали»

/* ---------- цвета букмекеров ---------- */

const BK_CLASS = {
  "Fonbet": "bk-fonbet",       // красный
  "BetBoom": "bk-betboom",     // синий
  "Winline": "bk-winline",     // оранжевый
  "Liga Stavok": "bk-liga",    // зелёный
  "Лига Ставок": "bk-liga",
  "bc.game": "bk-bcgame",      // фиолетовый
};
const bkClass = (bk) => BK_CLASS[bk] || "bk-other";
const bkChip = (bk) => `<span class="bk-chip ${bkClass(bk)}">${escapeHtml(bk)}</span>`;

/* ---------- состояние (сохраняется в localStorage) ---------- */

const state = {
  view: "arbs",                 // arbs | live | arbs1x2 | live1x2 | matches | live-matches
  search: "",
  sport: "",
  bookmaker: "",
  openMatch: null,              // id открытого матча (страница котировок)
  sort: {
    arbs: { key: "profit_pct", dir: -1 },
    live: { key: "profit_pct", dir: -1 },
    arbs1x2: { key: "profit_pct", dir: -1 },
    live1x2: { key: "profit_pct", dir: -1 },
    matches: { key: "start_ts", dir: 1 },
    "live-matches": { key: "sport", dir: 1 },
  },
};

const isArbView = (v) => v === "arbs" || v === "live";
const isArb1x2View = (v) => v === "arbs1x2" || v === "live1x2";
const isMatchesView = (v) => v === "matches" || v === "live-matches";
// лайв-режимы опрашиваются чаще и берут данные из /api/live/*
const isLiveView = (v) => v === "live" || v === "live-matches" || v === "live1x2";

function loadState() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem("arb-scanner-ui") || "{}") || {};
  } catch (_) { /* повреждённое хранилище — игнорируем */ }
  Object.assign(state, saved, { sort: { ...state.sort, ...(saved.sort || {}) } });
  if (!["arbs", "live", "arbs1x2", "live1x2", "matches", "live-matches"].includes(state.view)) state.view = "arbs";
  state.openMatch = null; // страница котировок не восстанавливается
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
let lastArbs1x2 = [];
let lastMatches = [];
let lastDetail = null;

// Строки текущего представления (для фильтров/сортировки/индикаторов)
function currentRows() {
  if (isArb1x2View(state.view)) return lastArbs1x2;
  if (isMatchesView(state.view)) return lastMatches;
  return lastArbs;
}
function currentTable() {
  if (isArb1x2View(state.view)) return els.arbs1x2Table;
  if (isMatchesView(state.view)) return els.matchesTable;
  return els.arbsTable;
}

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
    if (isMatchesView(state.view) && state.bookmaker &&
        !(r.bookmakers || []).includes(state.bookmaker)) return false;
    if (q) {
      // ВАЖНО: .toLowerCase() должен охватывать ВСЮ строку. Без внешних
      // скобок он применялся только ко второму литералу, а имена команд
      // (r.match) оставались в верхнем регистре — поиск «синнер» не находил
      // «Синнер». Оборачиваем всю склейку в скобки.
      const hay = (`${r.match} ${r.sport} ${r.market || ""} ` +
        `${(r.bookmakers || []).join(" ")} ${r.k1_bookmaker || ""} ${r.k2_bookmaker || ""} ${r.kx_bookmaker || ""}`).toLowerCase();
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
  const table = currentTable();
  const { key, dir } = state.sort[state.view];
  document.querySelectorAll("th.sortable").forEach((th) => th.classList.remove("sorted-asc", "sorted-desc"));
  table.querySelectorAll("th.sortable").forEach((th) => {
    if (th.dataset.sort === key) th.classList.add(dir === 1 ? "sorted-asc" : "sorted-desc");
  });
}

function refreshFilterOptions() {
  const rows = currentRows();
  // В фильтре — только корневые виды спорта, без лиг и росписей
  const sports = [...new Set(rows.map((r) => rootSport(r.sport)))].sort((a, b) => a.localeCompare(b, "ru"));
  fillSelect(els.sportFilter, sports, state.sport, "Все");
  if (isMatchesView(state.view)) {
    const bks = [...new Set(rows.flatMap((r) => r.bookmakers || []))].sort();
    fillSelect(els.bkFilter, bks, state.bookmaker, "Все");
  }
  els.bkFilterLabel.hidden = !isMatchesView(state.view);
  els.minProfitLabel.hidden = isMatchesView(state.view);
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

function startLabel(r) {
  if (r.kind === "live") return null;
  if (!r.start_ts && !r.start_time) return null;
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
  return label;
}

function startCell(r) {
  if (r.kind === "live") return '<span class="kind live-cell">🔴 LIVE</span>';
  const label = startLabel(r);
  if (!label) return '<span class="muted">—</span>';
  return `<span class="kind prematch">${escapeHtml(label)}</span>`;
}

/* Таймер жизни вилки: сколько времени она уже видна сканеру.
 * Сервер присылает first_seen (unix-время первого обнаружения); ячейки
 * с классом .arb-age обновляются раз в секунду без перерисовки таблицы. */
function fmtAge(ts) {
  if (!ts) return "—";
  let s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  s %= 60;
  const two = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(s)}` : `${m}:${two(s)}`;
}

setInterval(() => {
  document.querySelectorAll(".arb-age[data-first-seen]").forEach((el) => {
    const ts = parseFloat(el.dataset.firstSeen);
    if (ts) el.textContent = fmtAge(ts);
  });
}, 1000);

function renderMeta(shown, total) {
  els.tableMeta.textContent = shown === total
    ? `Показано: ${shown}`
    : `Показано: ${shown} из ${total} (фильтры активны)`;
}

function renderMatches() {
  const rows = applySort(applyFilters(lastMatches));
  renderMeta(rows.length, lastMatches.length);
  if (!rows.length) {
    els.matchesBody.innerHTML =
      '<tr><td colspan="5" class="empty">Ни одного матча под текущие фильтры — ждём окончания обхода БК…</td></tr>';
    return;
  }
  els.matchesBody.innerHTML = rows.map((m) => `<tr class="match-row" data-id="${escapeHtml(m.id)}">
      <td>${startCell(m)}</td>
      <td>${escapeHtml(m.sport)}</td>
      <td class="match-name">${escapeHtml(m.match)}</td>
      <td>${(m.bookmakers || []).map(bkChip).join(" ")}</td>
      <td class="num">${m.markets_count}</td>
    </tr>`).join("");
}

function renderDetail() {
  const d = lastDetail;
  if (!d) {
    els.detailTitle.textContent = "Матч не найден";
    els.detailMeta.textContent = "Возможно, матч уже начался или котировки устарели.";
    els.detailBody.innerHTML = '<tr><td colspan="4" class="empty">Нет данных</td></tr>';
    return;
  }
  els.detailTitle.textContent = d.match;
  const when = d.kind === "live" ? "🔴 LIVE" : (startLabel(d) || "");
  els.detailMeta.innerHTML =
    `${escapeHtml(d.sport)} · ${escapeHtml(when)} · ` +
    (d.bookmakers || []).map(bkChip).join(" ");
  renderMeta(d.markets.length, d.markets.length);

  if (!d.markets.length) {
    els.detailBody.innerHTML = '<tr><td colspan="4" class="empty">Котировок нет</td></tr>';
    return;
  }
  const cell = (m, side) => (m.quotes || []).map((q) => {
    const k = side === 1 ? q.k1 : side === 3 ? q.k3 : q.k2;
    if (k == null) return "";
    const body = `<span class="quote ${bkClass(q.bookmaker)}" title="${escapeHtml(q.bookmaker)}">` +
      `${k.toFixed(2)}</span>`;
    // кэф — ссылка на страницу события у этой БК (если известна)
    return q.url
      ? `<a class="quote-link" href="${escapeHtml(q.url)}" target="_blank" rel="noopener">${body}</a>`
      : body;
  }).join(" ");
  els.detailBody.innerHTML = d.markets.map((m) => `<tr>
      <td class="market-name">${escapeHtml(m.market)}</td>
      <td><span class="out">${escapeHtml(m.outcome1)}</span> ${cell(m, 1)}</td>
      <td>${m.outcome3 ? `<span class="out">${escapeHtml(m.outcome3)}</span> ${cell(m, 3)}` : '<span class="muted">—</span>'}</td>
      <td><span class="out">${escapeHtml(m.outcome2)}</span> ${cell(m, 2)}</td>
    </tr>`).join("");
}

function renderBkCounts(bookmakers) {
  const age = (s) => {
    if (s == null) return "";
    if (s < 90) return "только что";
    return `${Math.round(s / 60)} мин назад`;
  };
  const parts = Object.entries(bookmakers || {}).map(([bk, v]) => {
    const cls = bkClass(bk);
    if (typeof v === "number") return `<span class="${cls}">${escapeHtml(bk)}: ${v}</span>`;
    return `<span class="${cls}">${escapeHtml(bk)}: ${v.count}</span> <span class="muted">(${age(v.age_sec)})</span>`;
  });
  els.bkCounts.innerHTML = parts.length ? parts.join(" · ") : "";
}

/* Закреплённые вилки: клик по строке «пришпиливает» её к верху таблицы —
 * она стоит на месте и обведена, пока жива. Пропала из обновления —
 * исчезает из таблицы (и открепляется). Особенно нужно в лайве, где
 * список пересортировывается каждые 5 секунд. */
let pinnedKeys = [];

function togglePin(key) {
  const i = pinnedKeys.indexOf(key);
  if (i >= 0) pinnedKeys.splice(i, 1);
  else pinnedKeys.push(key);
  renderArbs();
}

function renderArbs() {
  const bank = els.bank.value;
  els.arbCount.textContent = `Вилок: ${lastArbs.length}`;
  const rows = applySort(applyFilters(lastArbs));

  // живые закреплённые — всегда сверху, в порядке закрепления и без
  // фильтров (закрепили — значит следите за ней); мёртвые открепляются
  const byKey = new Map(lastArbs.map((a) => [a.match_key, a]));
  pinnedKeys = pinnedKeys.filter((k) => byKey.has(k));
  const pinnedSet = new Set(pinnedKeys);
  const all = [
    ...pinnedKeys.map((k) => byKey.get(k)),
    ...rows.filter((a) => !pinnedSet.has(a.match_key)),
  ];
  renderMeta(all.length, lastArbs.length);

  if (!all.length) {
    els.body.innerHTML =
      '<tr><td colspan="12" class="empty">Вилок нет — ждём следующего обновления…</td></tr>';
    return;
  }

  els.body.innerHTML = all.map((a) => {
    const st = a.stakes[bank] || {};
    const pinned = pinnedSet.has(a.match_key);
    const cls = ["arb-row"];
    if (pinned) cls.push("pinned");
    if (a.profit_pct > soundAlertProfit) cls.push("hot");
    return `<tr class="${cls.join(" ")}" data-key="${escapeHtml(a.match_key)}"
        title="${pinned ? "Клик — открепить" : "Клик — закрепить вилку сверху"}">
      <td>${pinned ? "📌 " : ""}${startCell(a)}</td>
      <td>${escapeHtml(a.sport)}</td>
      <td>${escapeHtml(a.match)}</td>
      <td>${escapeHtml(a.market)}</td>
      <td><span class="out">${escapeHtml(a.outcome1)}</span> <span class="coef">${a.k1_max.toFixed(2)}</span> ${bkChip(a.k1_bookmaker)}</td>
      <td><span class="out">${escapeHtml(a.outcome2)}</span> <span class="coef">${a.k2_max.toFixed(2)}</span> ${bkChip(a.k2_bookmaker)}</td>
      <td class="profit">${a.profit_pct.toFixed(2)} %</td>
      <td class="num arb-age" data-first-seen="${a.first_seen || ""}">${fmtAge(a.first_seen)}</td>
      <td class="stake">${fmtMoney(st.stake1)} <span class="bk">${escapeHtml(a.outcome1)} · ${escapeHtml(a.k1_bookmaker)}</span></td>
      <td class="stake">${fmtMoney(st.stake2)} <span class="bk">${escapeHtml(a.outcome2)} · ${escapeHtml(a.k2_bookmaker)}</span></td>
      <td class="stake">+${fmtMoney(st.profit)}</td>
      <td><button type="button" class="bet-btn" data-key="${escapeHtml(a.match_key)}">Поставить</button></td>
    </tr>`;
  }).join("");
}

/* Закреплённые ТРЁХисходные вилки (свой список, аналогично pinnedKeys) */
let pinnedKeys3 = [];

function togglePin3(key) {
  const i = pinnedKeys3.indexOf(key);
  if (i >= 0) pinnedKeys3.splice(i, 1);
  else pinnedKeys3.push(key);
  renderArbs1x2();
}

function renderArbs1x2() {
  const bank = els.bank.value;
  const rows = applySort(applyFilters(lastArbs1x2));

  const byKey = new Map(lastArbs1x2.map((a) => [a.match_key, a]));
  pinnedKeys3 = pinnedKeys3.filter((k) => byKey.has(k));
  const pinnedSet = new Set(pinnedKeys3);
  const all = [
    ...pinnedKeys3.map((k) => byKey.get(k)),
    ...rows.filter((a) => !pinnedSet.has(a.match_key)),
  ];
  renderMeta(all.length, lastArbs1x2.length);

  if (!all.length) {
    els.body1x2.innerHTML =
      '<tr><td colspan="13" class="empty">Вилок 1X2 нет — ждём следующего обновления…</td></tr>';
    return;
  }

  els.body1x2.innerHTML = all.map((a) => {
    const st = a.stakes[bank] || {};
    const pinned = pinnedSet.has(a.match_key);
    const cls = ["arb-row"];
    if (pinned) cls.push("pinned");
    if (a.profit_pct > soundAlertProfit) cls.push("hot");
    return `<tr class="${cls.join(" ")}" data-key="${escapeHtml(a.match_key)}"
        title="${pinned ? "Клик — открепить" : "Клик — закрепить вилку сверху"}">
      <td>${pinned ? "📌 " : ""}${startCell(a)}</td>
      <td>${escapeHtml(a.sport)}</td>
      <td>${escapeHtml(a.match)}</td>
      <td><span class="out">П1</span> <span class="coef">${a.k1_max.toFixed(2)}</span> ${bkChip(a.k1_bookmaker)}</td>
      <td><span class="out">X</span> <span class="coef">${a.kx_max.toFixed(2)}</span> ${bkChip(a.kx_bookmaker)}</td>
      <td><span class="out">П2</span> <span class="coef">${a.k2_max.toFixed(2)}</span> ${bkChip(a.k2_bookmaker)}</td>
      <td class="profit">${a.profit_pct.toFixed(2)} %</td>
      <td class="num arb-age" data-first-seen="${a.first_seen || ""}">${fmtAge(a.first_seen)}</td>
      <td class="stake">${fmtMoney(st.stake1)}</td>
      <td class="stake">${fmtMoney(st.stakex)}</td>
      <td class="stake">${fmtMoney(st.stake2)}</td>
      <td class="stake">+${fmtMoney(st.profit)}</td>
      <td><button type="button" class="bet3-btn" data-key="${escapeHtml(a.match_key)}">Поставить</button></td>
    </tr>`;
  }).join("");
}

/* ---------- модалка быстрой ставки ---------- */

let betArb = null;   // вилка, открытая в модалке (обновляется при опросе)

function calcBetStakes(k1, k2, bank) {
  const s = 1 / k1 + 1 / k2;
  const stake1 = Math.round(bank * (1 / k1) / s);
  const stake2 = Math.max(0, Math.round(bank) - stake1);
  // выигрыш при любом исходе (минимум из двух плеч — из-за округления)
  const payout = Math.min(stake1 * k1, stake2 * k2);
  return { stake1, stake2, payout, profit: payout - bank };
}

function renderBetModal() {
  const a = betArb;
  if (!a) return;
  els.betTitle.textContent = a.match;
  const when = startLabel(a);
  els.betMeta.innerHTML = `${escapeHtml(a.sport)} · ${escapeHtml(a.market)}` +
    (when ? ` · ${escapeHtml(when)}` : "") +
    ` · доходность <span class="profit">${a.profit_pct.toFixed(2)} %</span>` +
    (a.first_seen
      ? ` · ⏱ живёт <span class="arb-age" data-first-seen="${a.first_seen}">${fmtAge(a.first_seen)}</span>`
      : "");

  const bank = Math.max(0, parseFloat(els.betBank.value) || 0);
  const st = calcBetStakes(a.k1_max, a.k2_max, bank);

  els.betOut1.textContent = a.outcome1;
  els.betOut2.textContent = a.outcome2;
  els.betK1.textContent = a.k1_max.toFixed(2);
  els.betK2.textContent = a.k2_max.toFixed(2);
  els.betBk1.innerHTML = bkChip(a.k1_bookmaker);
  els.betBk2.innerHTML = bkChip(a.k2_bookmaker);
  els.betStake1.textContent = fmtMoney(st.stake1);
  els.betStake2.textContent = fmtMoney(st.stake2);

  for (const [link, url] of [[els.betLink1, a.k1_url], [els.betLink2, a.k2_url]]) {
    if (url) { link.href = url; link.hidden = false; }
    else { link.removeAttribute("href"); link.hidden = true; }
  }

  els.betSummary.innerHTML = bank > 0
    ? `Выигрыш при любом исходе: <b>${fmtMoney(Math.round(st.payout))}</b> · ` +
      `чистая прибыль: <b class="profit">+${fmtMoney(Math.round(st.profit))}</b>`
    : "Укажите сумму ставки";
}

function openBetModal(matchKey) {
  const a = lastArbs.find((x) => x.match_key === matchKey);
  if (!a) return;
  betArb = a;
  els.betBank.value = els.bank.value; // стартуем от выбранного банка
  els.betModal.hidden = false;
  renderBetModal();
}

function closeBetModal() {
  betArb = null;
  els.betModal.hidden = true;
}

// при опросе кэфы могли измениться — обновляем открытую модалку;
// если вилка исчезла, честно сообщаем (ставить по старым кэфам нельзя)
function refreshBetModal() {
  if (!betArb) return;
  const fresh = lastArbs.find((x) => x.match_key === betArb.match_key);
  if (fresh) {
    betArb = fresh;
    renderBetModal();
  } else {
    els.betSummary.innerHTML =
      '<span class="bet-gone">⚠ Вилка пропала из последнего обновления — кэфы изменились, не ставьте.</span>';
  }
}

els.body.addEventListener("click", (e) => {
  const btn = e.target.closest("button.bet-btn");
  if (btn) {
    openBetModal(btn.dataset.key);
    return;
  }
  // клик по строке (не по кнопке/ссылке) — закрепить/открепить вилку
  const tr = e.target.closest("tr.arb-row");
  if (tr && !e.target.closest("a")) togglePin(tr.dataset.key);
});

els.betClose.addEventListener("click", closeBetModal);
els.betModal.addEventListener("click", (e) => {
  if (e.target === els.betModal) closeBetModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.betModal.hidden) closeBetModal();
});

els.betBank.addEventListener("input", renderBetModal);

els.betOpenBoth.addEventListener("click", () => {
  if (!betArb) return;
  // открываем обе страницы; если браузер заблокировал всплывающие окна,
  // остаются прямые ссылки «Открыть БК» в каждом плече
  if (betArb.k2_url) window.open(betArb.k2_url, "_blank", "noopener");
  if (betArb.k1_url) window.open(betArb.k1_url, "_blank", "noopener");
});

document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!betArb) return;
    const bank = Math.max(0, parseFloat(els.betBank.value) || 0);
    const st = calcBetStakes(betArb.k1_max, betArb.k2_max, bank);
    const val = btn.dataset.copy === "1" ? st.stake1 : st.stake2;
    navigator.clipboard?.writeText(String(val)).then(() => {
      const orig = btn.textContent;
      btn.textContent = "✓ Скопировано";
      setTimeout(() => { btn.textContent = orig; }, 1200);
    });
  });
});

/* ---------- модалка быстрой ставки: ТРЁХисходная вилка (1X2) ---------- */

let bet3Arb = null;

function calcBetStakes3(k1, kx, k2, bank) {
  const s = 1 / k1 + 1 / kx + 1 / k2;
  const stake1 = Math.round(bank * (1 / k1) / s);
  const stakex = Math.round(bank * (1 / kx) / s);
  const stake2 = Math.max(0, Math.round(bank) - stake1 - stakex);
  const payout = Math.min(stake1 * k1, stakex * kx, stake2 * k2);
  return { stake1, stakex, stake2, payout, profit: payout - bank };
}

function renderBet3Modal() {
  const a = bet3Arb;
  if (!a) return;
  els.bet3Title.textContent = a.match;
  const when = startLabel(a);
  els.bet3Meta.innerHTML = `${escapeHtml(a.sport)} · Исход (1X2)` +
    (when ? ` · ${escapeHtml(when)}` : "") +
    ` · доходность <span class="profit">${a.profit_pct.toFixed(2)} %</span>` +
    (a.first_seen
      ? ` · ⏱ живёт <span class="arb-age" data-first-seen="${a.first_seen}">${fmtAge(a.first_seen)}</span>`
      : "");

  const bank = Math.max(0, parseFloat(els.bet3Bank.value) || 0);
  const st = calcBetStakes3(a.k1_max, a.kx_max, a.k2_max, bank);

  els.bet3K1.textContent = a.k1_max.toFixed(2);
  els.bet3Kx.textContent = a.kx_max.toFixed(2);
  els.bet3K2.textContent = a.k2_max.toFixed(2);
  els.bet3Bk1.innerHTML = bkChip(a.k1_bookmaker);
  els.bet3Bkx.innerHTML = bkChip(a.kx_bookmaker);
  els.bet3Bk2.innerHTML = bkChip(a.k2_bookmaker);
  els.bet3Stake1.textContent = fmtMoney(st.stake1);
  els.bet3Stakex.textContent = fmtMoney(st.stakex);
  els.bet3Stake2.textContent = fmtMoney(st.stake2);

  for (const [link, url] of [[els.bet3Link1, a.k1_url], [els.bet3Linkx, a.kx_url], [els.bet3Link2, a.k2_url]]) {
    if (url) { link.href = url; link.hidden = false; }
    else { link.removeAttribute("href"); link.hidden = true; }
  }

  els.bet3Summary.innerHTML = bank > 0
    ? `Выигрыш при любом исходе: <b>${fmtMoney(Math.round(st.payout))}</b> · ` +
      `чистая прибыль: <b class="profit">+${fmtMoney(Math.round(st.profit))}</b>`
    : "Укажите сумму ставки";
}

function openBet3Modal(matchKey) {
  const a = lastArbs1x2.find((x) => x.match_key === matchKey);
  if (!a) return;
  bet3Arb = a;
  els.bet3Bank.value = els.bank.value;
  els.bet3Modal.hidden = false;
  renderBet3Modal();
}

function closeBet3Modal() {
  bet3Arb = null;
  els.bet3Modal.hidden = true;
}

function refreshBet3Modal() {
  if (!bet3Arb) return;
  const fresh = lastArbs1x2.find((x) => x.match_key === bet3Arb.match_key);
  if (fresh) {
    bet3Arb = fresh;
    renderBet3Modal();
  } else {
    els.bet3Summary.innerHTML =
      '<span class="bet-gone">⚠ Вилка пропала из последнего обновления — кэфы изменились, не ставьте.</span>';
  }
}

els.body1x2.addEventListener("click", (e) => {
  const btn = e.target.closest("button.bet3-btn");
  if (btn) {
    openBet3Modal(btn.dataset.key);
    return;
  }
  const tr = e.target.closest("tr.arb-row");
  if (tr && !e.target.closest("a")) togglePin3(tr.dataset.key);
});

els.bet3Close.addEventListener("click", closeBet3Modal);
els.bet3Modal.addEventListener("click", (e) => {
  if (e.target === els.bet3Modal) closeBet3Modal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !els.bet3Modal.hidden) closeBet3Modal();
});

els.bet3Bank.addEventListener("input", renderBet3Modal);

els.bet3OpenAll.addEventListener("click", () => {
  if (!bet3Arb) return;
  if (bet3Arb.k2_url) window.open(bet3Arb.k2_url, "_blank", "noopener");
  if (bet3Arb.kx_url) window.open(bet3Arb.kx_url, "_blank", "noopener");
  if (bet3Arb.k1_url) window.open(bet3Arb.k1_url, "_blank", "noopener");
});

document.querySelectorAll(".copy-btn3").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!bet3Arb) return;
    const bank = Math.max(0, parseFloat(els.bet3Bank.value) || 0);
    const st = calcBetStakes3(bet3Arb.k1_max, bet3Arb.kx_max, bet3Arb.k2_max, bank);
    const val = btn.dataset.copy === "1" ? st.stake1 : btn.dataset.copy === "x" ? st.stakex : st.stake2;
    navigator.clipboard?.writeText(String(val)).then(() => {
      const orig = btn.textContent;
      btn.textContent = "✓ Скопировано";
      setTimeout(() => { btn.textContent = orig; }, 1200);
    });
  });
});

function updateVisibility() {
  const detailOpen = isMatchesView(state.view) && state.openMatch !== null;
  els.arbsTable.hidden = !isArbView(state.view);
  els.arbs1x2Table.hidden = !isArb1x2View(state.view);
  els.matchesTable.hidden = !isMatchesView(state.view) || detailOpen;
  els.matchDetail.hidden = !detailOpen;
}

function rerender() {
  refreshFilterOptions();
  updateSortIndicators();
  updateVisibility();
  if (isMatchesView(state.view)) {
    if (state.openMatch !== null) renderDetail();
    else renderMatches();
  } else if (isArb1x2View(state.view)) {
    renderArbs1x2();
  } else {
    renderArbs();
  }
}

/* ---------- опрос API ---------- */

async function poll() {
  const live = isLiveView(state.view);
  const arbsUrl = live ? "/api/live/arbs" : "/api/arbs";
  try {
    const minProfit = parseFloat(els.minProfit.value) || 0;
    const resp = await fetch(`${arbsUrl}?min_profit=${minProfit}`);
    const data = await resp.json();

    soundAlertProfit = data.sound_alert_profit ?? 2.5;
    els.soundThreshold.textContent = soundAlertProfit;
    els.interval.textContent = data.scan_interval;

    els.modeBadge.textContent = live ? "ЛАЙВ" : "ПРЕМАТЧ";
    els.modeBadge.className = live ? "badge hot-badge" : "badge live";
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
    lastArbs1x2 = data.arbs_1x2 || [];
    els.arbCount.textContent = isArb1x2View(state.view)
      ? `Вилок 1X2: ${lastArbs1x2.length}`
      : `Вилок: ${lastArbs.length}`;
    refreshBetModal();
    refreshBet3Modal();

    // Звук — только для НОВЫХ вилок (в режимах вилок, не в списке матчей)
    if (isArbView(state.view) || isArb1x2View(state.view)) {
      const relevant = isArb1x2View(state.view) ? lastArbs1x2 : data.arbs;
      const hot = relevant.filter((a) => a.profit_pct > soundAlertProfit);
      const fresh = hot.filter((a) => !alertedKeys.has(a.match_key));
      if (fresh.length && els.soundOn.checked) beep();
      alertedKeys = new Set(hot.map((a) => a.match_key));
    }
  } catch (err) {
    els.lastScan.textContent = "Ошибка связи с сервером…";
  }

  if (isMatchesView(state.view)) {
    if (state.openMatch !== null) {
      await pollDetail();
    } else {
      const url = state.view === "live-matches" ? "/api/live/matches" : "/api/matches";
      try {
        const resp = await fetch(url);
        const data = await resp.json();
        lastMatches = data.matches;
      } catch (err) { /* статус уже показан выше */ }
    }
  }
  rerender();
}

async function pollDetail() {
  if (state.openMatch === null) return;
  const live = state.view === "live-matches" ? 1 : 0;
  try {
    const resp = await fetch(`/api/match?id=${encodeURIComponent(state.openMatch)}&live=${live}`);
    const data = await resp.json();
    lastDetail = data.match;
  } catch (err) {
    lastDetail = null;
  }
}

function openMatch(id) {
  state.openMatch = id;
  lastDetail = null;
  updateVisibility();
  els.detailTitle.textContent = "Загрузка…";
  els.detailMeta.textContent = "";
  els.detailBody.innerHTML = '<tr><td colspan="4" class="empty">Загрузка котировок…</td></tr>';
  pollDetail().then(rerender);
}

function closeMatch() {
  state.openMatch = null;
  lastDetail = null;
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
  state.openMatch = null;
  saveState();
  els.viewTabs.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("active", b === btn));
  updateVisibility();
  restartPolling();
  poll();
});

els.matchesBody.addEventListener("click", (e) => {
  const row = e.target.closest("tr.match-row");
  if (row && row.dataset.id) openMatch(row.dataset.id);
});

els.backBtn.addEventListener("click", closeMatch);

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    const sort = state.sort[state.view];
    if (sort.key === key) sort.dir = -sort.dir;
    else {
      sort.key = key;
      // числовые — сначала по убыванию, текст/время — по возрастанию
      sort.dir = ["profit_pct", "k1_max", "kx_max", "k2_max", "markets_count"].includes(key) ? -1 : 1;
    }
    saveState();
    rerender();
  });
});

/* ---------- запуск ---------- */

let pollTimer = null;
function restartPolling() {
  if (pollTimer) clearInterval(pollTimer);
  const ms = isLiveView(state.view) ? LIVE_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
  pollTimer = setInterval(poll, ms);
}

loadState();
els.viewTabs.querySelectorAll("button").forEach(
  (b) => b.classList.toggle("active", b.dataset.view === state.view));
updateVisibility();

poll();
restartPolling();
