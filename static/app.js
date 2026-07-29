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
  // карточки-обёртки таблиц: скрывать надо их, иначе от спрятанной таблицы
  // остаётся пустая рамка
  arbsCard: document.getElementById("arbs-card"),
  arbs1x2Card: document.getElementById("arbs1x2-card"),
  matchesCard: document.getElementById("matches-card"),
  matchDetail: document.getElementById("match-detail"),
  detailTitle: document.getElementById("detail-title"),
  detailMeta: document.getElementById("detail-meta"),
  detailBody: document.getElementById("detail-body"),
  backBtn: document.getElementById("back-btn"),
  viewTabs: document.getElementById("view-tabs"),
  search: document.getElementById("search"),
  sportFilter: document.getElementById("sport-filter"),
  bkFilter: document.getElementById("bk-filter"),
  resetFilters: document.getElementById("reset-filters"),
  filters: document.querySelector(".filters"),
  filtersToggle: document.getElementById("filters-toggle"),
  toastHost: document.getElementById("toast-host"),
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
  betLimit: document.getElementById("bet-limit"),
  betOpenBoth: document.getElementById("bet-open-both"),
  betAutobet: document.getElementById("bet-autobet"),
  betAutobetResult: document.getElementById("bet-autobet-result"),
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
  bet3Limit: document.getElementById("bet3-limit"),
  bet3OpenAll: document.getElementById("bet3-open-all"),
  bet3Autobet: document.getElementById("bet3-autobet"),
  bet3AutobetResult: document.getElementById("bet3-autobet-result"),
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
  "LeonBet": "bk-leon",        // жёлтый
  "Betcity": "bk-betcity",     // голубой (циан)
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
// Все БК, которые сейчас опрашивает сканер (из /api/arbs). Список фильтра
// строим по нему, а не только по видимым строкам: иначе выбранная БК
// исчезала бы из фильтра всякий раз, когда по ней нет вилок.
let lastBkNames = [];

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
  let va = a[key], vb = b[key];
  // список БК матча сравниваем как строку «БК1, БК2» — так матчи одного
  // набора букмекеров встают рядом
  if (Array.isArray(va)) va = [...va].sort().join(", ");
  if (Array.isArray(vb)) vb = [...vb].sort().join(", ");
  if (va == null && vb == null) return 0;
  if (va == null) return 1;   // пустые — в конец
  if (vb == null) return -1;
  if (typeof va === "number" && typeof vb === "number") return va - vb;
  return String(va).localeCompare(String(vb), "ru");
}

// Корневой вид спорта: «Футбол · США. MLS» -> «Футбол»
const rootSport = (s) => String(s).split(" · ")[0].trim();

/* Ячейка спорта: вид спорта заметно, турнир — второй строкой и мельче.
 * Одной строкой «Киберспорт · Marvel Rivals. Ignite: Mid Season Finals. Bo3»
 * занимала пол-таблицы, а глазом всё равно ищут вид спорта. */
function sportCell(sport) {
  const [root, ...rest] = String(sport).split(" · ");
  const league = rest.join(" · ").trim();
  return `<span class="sport-root">${escapeHtml(root)}</span>` +
    (league ? `<span class="sport-league">${escapeHtml(league)}</span>` : "");
}

// Все БК, участвующие в строке: у матча — список БК, у вилки — БК её плеч
// (в т.ч. третьего, ничейного, у вилок 1X2).
function rowBookmakers(r) {
  if (r.bookmakers) return r.bookmakers;
  return [r.k1_bookmaker, r.kx_bookmaker, r.k2_bookmaker].filter(Boolean);
}

function applyFilters(rows) {
  const q = state.search.trim().toLowerCase();
  return rows.filter((r) => {
    if (state.sport && rootSport(r.sport) !== state.sport) return false;
    if (state.bookmaker && !rowBookmakers(r).includes(state.bookmaker)) return false;
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
  const cls = dir === 1 ? "sorted-asc" : "sorted-desc";
  document.querySelectorAll("th.sortable, .sort-bk").forEach(
    (el) => el.classList.remove("sorted-asc", "sorted-desc"));
  table.querySelectorAll("[data-sort]").forEach((el) => {
    if (el.dataset.sort === key) el.classList.add(cls);
  });
}

function refreshFilterOptions() {
  const rows = currentRows();
  // В фильтре — только корневые виды спорта, без лиг и росписей
  const sports = [...new Set(rows.map((r) => rootSport(r.sport)))].sort((a, b) => a.localeCompare(b, "ru"));
  fillSelect(els.sportFilter, sports, state.sport, "Все");
  // Фильтр по БК работает во ВСЕХ вкладках: в списке вилок оставляет те, у
  // которых выбранная БК стоит хотя бы в одном плече (удобно смотреть
  // вилки только по тем БК, где есть аккаунт).
  const bks = [...new Set([...lastBkNames, ...rows.flatMap(rowBookmakers)])].sort();
  fillSelect(els.bkFilter, bks, state.bookmaker, "Все");
  els.minProfitLabel.hidden = isMatchesView(state.view);
}

function fillSelect(sel, values, current, allLabel) {
  // выбранное значение оставляем в списке даже если под него сейчас нет
  // строк — иначе фильтр молча сбрасывался бы сам
  const all = current && !values.includes(current) ? [...values, current] : values;
  sel.innerHTML = `<option value="">${allLabel}</option>` +
    all.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  sel.value = current || "";
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ---------- отрисовка ---------- */

const fmtMoney = (n) => Number(n).toLocaleString("ru-RU") + " ₽";
// В таблице рубли выносим в заголовок столбца («Ставка 1, ₽»): 12 столбцов
// и без повторяющегося знака еле влезают в ноутбучный экран.
const fmtNum = (n) => Number(n).toLocaleString("ru-RU");

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
      <td data-label="Начало">${startCell(m)}</td>
      <td data-label="Спорт" class="sport">${sportCell(m.sport)}</td>
      <td data-label="Матч" class="match-name">${escapeHtml(m.match)}</td>
      <td data-label="БК">${(m.bookmakers || []).map(bkChip).join(" ")}</td>
      <td data-label="Рынков" class="num">${m.markets_count}</td>
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
  // «Показано: N из M» тут не к месту: фильтры к росписи одного матча не
  // применяются (и сама панель фильтров на это время спрятана)
  els.tableMeta.textContent = `Рынков: ${d.markets.length}`;

  if (!d.markets.length) {
    els.detailBody.innerHTML = '<tr><td colspan="4" class="empty">Котировок нет</td></tr>';
    return;
  }
  // Роспись нужна ровно для того, чтобы сравнить кэфы БК между собой,
  // поэтому у каждого кэфа подписана его БК (одного цвета мало: Fonbet и
  // Winline на глаз похожи, а на телефоне подсказки по наведению нет), а
  // лучший кэф исхода выделен — именно на него и ставят.
  const cell = (m, side) => {
    const list = (m.quotes || [])
      .map((q) => ({ bk: q.bookmaker, url: q.url,
                     k: side === 1 ? q.k1 : side === 3 ? q.k3 : q.k2 }))
      .filter((q) => q.k != null);
    const best = Math.max(...list.map((q) => q.k));
    return list.map((q) => {
      const top = list.length > 1 && q.k === best ? " best" : "";
      const body = `<span class="quote ${bkClass(q.bk)}${top}" title="${escapeHtml(q.bk)}">` +
        `${q.k.toFixed(2)}<span class="quote-bk">${escapeHtml(q.bk)}</span></span>`;
      // кэф — ссылка на страницу события у этой БК (если известна)
      return q.url
        ? `<a class="quote-link" href="${escapeHtml(q.url)}" target="_blank" rel="noopener">${body}</a>`
        : body;
    }).join(" ");
  };
  els.detailBody.innerHTML = d.markets.map((m) => `<tr>
      <td data-label="Рынок" class="market-name">${escapeHtml(m.market)}</td>
      <td data-label="Исход 1"><span class="out">${escapeHtml(m.outcome1)}</span> ${cell(m, 1)}</td>
      <td data-label="Ничья">${m.outcome3 ? `<span class="out">${escapeHtml(m.outcome3)}</span> ${cell(m, 3)}` : '<span class="muted">—</span>'}</td>
      <td data-label="Исход 2"><span class="out">${escapeHtml(m.outcome2)}</span> ${cell(m, 2)}</td>
    </tr>`).join("");
}

function renderBkCounts(bookmakers) {
  const age = (s) => {
    if (s == null) return "";
    if (s < 90) return "только что";
    return `${Math.round(s / 60)} мин назад`;
  };
  // Каждая БК обновляется в своём темпе (медленная не тормозит быструю).
  // Возраст котировок показываем всегда, а у той, которую опрашивают прямо
  // сейчас, добавляем пометку: обход БК идёт десятки секунд, и без неё
  // непонятно, БК «зависла» или как раз качает линию. Выключенную в админке
  // БК видно отдельно — иначе её исчезновение из шапки выглядит как сбой.
  const chips = Object.entries(bookmakers || {}).map(([bk, v]) => {
    const cls = bkClass(bk);
    const count = typeof v === "number" ? v : v.count;
    const state = typeof v === "number" ? {} : v;
    const note = state.off
      ? "выключена"
      : [state.count ? age(state.age_sec) : "", state.busy ? "обновляется…" : ""]
        .filter(Boolean).join(", ");
    const extra = [state.busy ? "busy" : "", state.off ? "off" : ""].join(" ").trim();
    return `<span class="bk-fresh ${cls} ${extra}" title="${escapeHtml(bk)}">
        <span class="dot"></span>${escapeHtml(bk)}
        <span class="bk-count">${count}</span>
        ${note ? `<span class="bk-note">${note}</span>` : ""}
      </span>`;
  });
  els.bkCounts.innerHTML = chips.join("");
}

/* Всплывающие плашки вместо alert(): о таких мелочах, как «сумма
 * скопирована» или «сервер не ответил», незачем спрашивать разрешения. */
function toast(message, kind = "") {
  if (!els.toastHost) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`.trim();
  el.textContent = message;
  els.toastHost.appendChild(el);
  setTimeout(() => el.remove(), 3200);
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
  els.arbCount.innerHTML = `Вилок <b>${lastArbs.length}</b>`;
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
    // data-label подписывает ячейку на телефоне, где таблица превращается
    // в карточки (см. @media в style.css)
    return `<tr class="${cls.join(" ")}" data-key="${escapeHtml(a.match_key)}"
        title="${pinned ? "Клик — открепить" : "Клик — закрепить вилку сверху"}">
      <td data-label="Начало">${pinned ? "📌 " : ""}${startCell(a)}</td>
      <td data-label="Спорт" class="sport">${sportCell(a.sport)}</td>
      <td data-label="Матч" class="match-name">${escapeHtml(a.match)}</td>
      <td data-label="Рынок">${escapeHtml(a.market)}</td>
      <td data-label="Исход 1"><span class="out">${escapeHtml(a.outcome1)}</span> <span class="coef">${a.k1_max.toFixed(2)}</span> ${bkChip(a.k1_bookmaker)}</td>
      <td data-label="Исход 2"><span class="out">${escapeHtml(a.outcome2)}</span> <span class="coef">${a.k2_max.toFixed(2)}</span> ${bkChip(a.k2_bookmaker)}</td>
      <td data-label="Доходность"><span class="profit">${a.profit_pct.toFixed(2)} %</span></td>
      <td data-label="Живёт" class="num arb-age" data-first-seen="${a.first_seen || ""}">${fmtAge(a.first_seen)}</td>
      <td data-label="Ставка 1" class="stake">${fmtNum(st.stake1)}</td>
      <td data-label="Ставка 2" class="stake">${fmtNum(st.stake2)}</td>
      <td data-label="Прибыль" class="stake profit-cell">+${fmtNum(st.profit)}</td>
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
      <td data-label="Начало">${pinned ? "📌 " : ""}${startCell(a)}</td>
      <td data-label="Спорт" class="sport">${sportCell(a.sport)}</td>
      <td data-label="Матч" class="match-name">${escapeHtml(a.match)}</td>
      <td data-label="П1"><span class="out">П1</span> <span class="coef">${a.k1_max.toFixed(2)}</span> ${bkChip(a.k1_bookmaker)}</td>
      <td data-label="X"><span class="out">X</span> <span class="coef">${a.kx_max.toFixed(2)}</span> ${bkChip(a.kx_bookmaker)}</td>
      <td data-label="П2"><span class="out">П2</span> <span class="coef">${a.k2_max.toFixed(2)}</span> ${bkChip(a.k2_bookmaker)}</td>
      <td data-label="Доходность"><span class="profit">${a.profit_pct.toFixed(2)} %</span></td>
      <td data-label="Живёт" class="num arb-age" data-first-seen="${a.first_seen || ""}">${fmtAge(a.first_seen)}</td>
      <td data-label="Ставка П1" class="stake">${fmtNum(st.stake1)}</td>
      <td data-label="Ставка X" class="stake">${fmtNum(st.stakex)}</td>
      <td data-label="Ставка П2" class="stake">${fmtNum(st.stake2)}</td>
      <td data-label="Прибыль" class="stake profit-cell">+${fmtNum(st.profit)}</td>
      <td><button type="button" class="bet3-btn" data-key="${escapeHtml(a.match_key)}">Поставить</button></td>
    </tr>`;
  }).join("");
}

/* ---------- модалка быстрой ставки ---------- */

/* Копирует сумму плеча в буфер: кнопка коротко подтверждает результат, а
 * при отказе браузера (без https буфер недоступен) сумма показывается
 * плашкой — иначе клик выглядел бы как «ничего не произошло». */
function copyStake(btn, value) {
  const done = () => {
    const orig = btn.textContent;
    btn.textContent = "✓ Скопировано";
    setTimeout(() => { btn.textContent = orig; }, 1200);
  };
  const fail = () => toast(`Скопируйте сумму вручную: ${value} ₽`, "warn");
  if (!navigator.clipboard) return fail();
  navigator.clipboard.writeText(String(value)).then(done, fail);
}

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

  renderLimit(els.betLimit, a.max_stake);
  els.betAutobet.hidden = !isLiveView(state.view);
  els.betAutobetResult.hidden = true;
}

/* Лимит ставки (max_stake) — минимальный известный баланс среди БК ноги
 * вилки. Считается на сервере из аккаунтов, подключённых в админке. */
function renderLimit(el, maxStake) {
  if (maxStake === null || maxStake === undefined) {
    el.hidden = false;
    el.innerHTML = '<span class="limit-unknown">Лимит ставки неизвестен — ' +
      'подключите аккаунты БК в <a href="/admin" target="_blank">админке</a>.</span>';
    return;
  }
  el.hidden = false;
  el.innerHTML = `Лимит по балансу подключённых аккаунтов: ` +
    `<span class="limit-known">${fmtMoney(Math.round(maxStake))}</span>`;
}

async function autobetApi(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await resp.json(); } catch (_) { /* ignore */ }
  if (!resp.ok) {
    const detail = data.detail || resp.statusText;
    if (resp.status === 401) {
      throw new Error(`Требуется вход в админку — откройте /admin и авторизуйтесь.`);
    }
    throw new Error(detail);
  }
  return data;
}

function renderAutobetResult(el, res) {
  el.hidden = false;
  const modeLabel = res.dry_run
    ? '<b style="color:var(--hot)">РЕЖИМ ИМИТАЦИИ</b> (реальный запрос не отправлялся)'
    : '<b style="color:#ef4444">РЕАЛЬНАЯ СТАВКА</b>';
  const legsHtml = res.legs.map((l) =>
    `<div class="${l.ok ? "leg-ok" : "leg-fail"}">${l.ok ? "✓" : "✗"} ` +
    `${escapeHtml(l.bookmaker)} · ${escapeHtml(l.outcome)} · ${fmtMoney(l.stake)}@${l.odds} — ` +
    `${escapeHtml(l.message)}</div>`).join("");
  const note = res.note ? `<div class="leg-fail">${escapeHtml(res.note)}</div>` : "";
  el.innerHTML = `${modeLabel}<br>${legsHtml}${note}`;
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
    els.betAutobet.hidden = true;
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

els.betAutobet.addEventListener("click", async () => {
  if (!betArb) return;
  els.betAutobet.disabled = true;
  els.betAutobet.textContent = "Ставим…";
  try {
    const res = await autobetApi("/api/autobet/place", {
      match_key: betArb.match_key, kind3: false, live: true,
    });
    renderAutobetResult(els.betAutobetResult, res);
  } catch (err) {
    els.betAutobetResult.hidden = false;
    els.betAutobetResult.innerHTML = `<span class="leg-fail">${escapeHtml(err.message)}</span>`;
  } finally {
    els.betAutobet.disabled = false;
    els.betAutobet.textContent = "⚡ Авто-ставка (лайв)";
  }
});

document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!betArb) return;
    const bank = Math.max(0, parseFloat(els.betBank.value) || 0);
    const st = calcBetStakes(betArb.k1_max, betArb.k2_max, bank);
    const val = btn.dataset.copy === "1" ? st.stake1 : st.stake2;
    copyStake(btn, val);
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

  renderLimit(els.bet3Limit, a.max_stake);
  els.bet3Autobet.hidden = !isLiveView(state.view);
  els.bet3AutobetResult.hidden = true;
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
    els.bet3Autobet.hidden = true;
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

els.bet3Autobet.addEventListener("click", async () => {
  if (!bet3Arb) return;
  els.bet3Autobet.disabled = true;
  els.bet3Autobet.textContent = "Ставим…";
  try {
    const res = await autobetApi("/api/autobet/place", {
      match_key: bet3Arb.match_key, kind3: true, live: true,
    });
    renderAutobetResult(els.bet3AutobetResult, res);
  } catch (err) {
    els.bet3AutobetResult.hidden = false;
    els.bet3AutobetResult.innerHTML = `<span class="leg-fail">${escapeHtml(err.message)}</span>`;
  } finally {
    els.bet3Autobet.disabled = false;
    els.bet3Autobet.textContent = "⚡ Авто-ставка (лайв)";
  }
});

document.querySelectorAll(".copy-btn3").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!bet3Arb) return;
    const bank = Math.max(0, parseFloat(els.bet3Bank.value) || 0);
    const st = calcBetStakes3(bet3Arb.k1_max, bet3Arb.kx_max, bet3Arb.k2_max, bank);
    const val = btn.dataset.copy === "1" ? st.stake1 : btn.dataset.copy === "x" ? st.stakex : st.stake2;
    copyStake(btn, val);
  });
});

function updateVisibility() {
  const detailOpen = isMatchesView(state.view) && state.openMatch !== null;
  // В росписи одного матча фильтры и банк ни на что не влияют — панель
  // только сбивала бы с толку («Показано: 7454» над таблицей одного матча)
  els.filters.hidden = detailOpen;
  els.filtersToggle.hidden = detailOpen;
  els.arbsCard.hidden = !isArbView(state.view);
  els.arbs1x2Card.hidden = !isArb1x2View(state.view);
  els.matchesCard.hidden = !isMatchesView(state.view) || detailOpen;
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
    // сессия сайта истекла (сайт закрыт паролем) — опрашивать дальше нечего
    if (resp.status === 401) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname));
      return;
    }
    const data = await resp.json();

    soundAlertProfit = data.sound_alert_profit ?? 2.5;
    els.soundThreshold.textContent = soundAlertProfit;
    els.interval.textContent = data.scan_interval;

    els.modeBadge.textContent = live ? "ЛАЙВ" : "ПРЕМАТЧ";
    els.modeBadge.className = live ? "badge hot-badge" : "badge live";
    els.scanningBadge.hidden = !data.scanning;

    if (data.last_scan) {
      els.lastScan.innerHTML = "Обновлено <b>" +
        new Date(data.last_scan * 1000).toLocaleTimeString("ru-RU") + "</b>";
    }

    if (data.events_checked) {
      els.coverage.innerHTML =
        `В линии <b>${data.events_checked.toLocaleString("ru-RU")}</b> событий, ` +
        `<b>${data.quotes_checked.toLocaleString("ru-RU")}</b> котировок`;
    }
    renderBkCounts(data.bookmakers);
    lastBkNames = Object.keys(data.bookmakers || {});

    lastArbs = data.arbs;
    lastArbs1x2 = data.arbs_1x2 || [];
    els.arbCount.innerHTML = isArb1x2View(state.view)
      ? `Вилок 1X2 <b>${lastArbs1x2.length}</b>`
      : `Вилок <b>${lastArbs.length}</b>`;
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
    els.lastScan.textContent = "Нет связи с сервером…";
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
  els.tableMeta.textContent = "";
  els.detailBody.innerHTML = '<tr><td colspan="4" class="empty">Загрузка котировок…</td></tr>';
  pollDetail().then(rerender);
}

function closeMatch() {
  state.openMatch = null;
  lastDetail = null;
  rerender();
}

/* ---------- обработчики ---------- */

els.minProfit.addEventListener("change", () => { saveState(); restartPolling(); });
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

// Свёрнутые фильтры на телефоне (на широком экране кнопка скрыта стилями)
els.filtersToggle.addEventListener("click", () => {
  const open = els.filtersToggle.getAttribute("aria-expanded") === "true";
  els.filtersToggle.setAttribute("aria-expanded", open ? "false" : "true");
});

els.resetFilters.addEventListener("click", () => {
  state.search = "";
  state.sport = "";
  state.bookmaker = "";
  els.search.value = "";
  els.minProfit.value = "0";
  saveState();
  restartPolling();
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
});

els.matchesBody.addEventListener("click", (e) => {
  const row = e.target.closest("tr.match-row");
  if (row && row.dataset.id) openMatch(row.dataset.id);
});

els.backBtn.addEventListener("click", closeMatch);

// Сортировка по клику на заголовок. Слушаем на таблицах целиком, чтобы
// работали и вложенные цели — подпись «(БК)» внутри заголовка кэфа
// сортирует по букмекеру плеча, а не по величине коэффициента.
const DESC_FIRST_KEYS = ["profit_pct", "k1_max", "kx_max", "k2_max",
                         "markets_count"];

document.querySelectorAll("table thead").forEach((thead) => {
  thead.addEventListener("click", (e) => {
    const target = e.target.closest("[data-sort]");
    if (!target || !thead.contains(target)) return;
    const key = target.dataset.sort;
    const sort = state.sort[state.view];
    if (sort.key === key) sort.dir = -sort.dir;
    else {
      sort.key = key;
      // числовые — сначала по убыванию, текст/время — по возрастанию
      sort.dir = DESC_FIRST_KEYS.includes(key) ? -1 : 1;
    }
    saveState();
    rerender();
  });
});

/* ---------- запуск ---------- */

/* Опрашиваем сервер по кругу, а не по таймеру: полный список матчей на
 * живой линии собирается секунды (десятки тысяч событий), и setInterval
 * успевал послать следующий запрос, не дождавшись предыдущего. Очередь
 * выедала лимит браузера на одновременные соединения к одному хосту —
 * после этого не отвечала вся страница, вплоть до перехода в админку. */
let pollTimer = null;
let polling = false;
// Просили обновиться, пока круг ещё шёл: сменили вкладку или фильтр — данные
// нужны от другого адреса, ждать полный интервал незачем
let pollAgain = false;

async function pollLoop() {
  if (polling) {
    pollAgain = true;
    return;
  }
  polling = true;
  pollAgain = false;
  try {
    // Скрытая вкладка (телефон в кармане, другой таб) ничего не показывает,
    // и дёргать из неё сервер незачем.
    if (!document.hidden) await poll();
  } finally {
    polling = false;
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollLoop, pollAgain ? 0 : (isLiveView(state.view)
      ? LIVE_POLL_INTERVAL_MS : POLL_INTERVAL_MS));
  }
}

function restartPolling() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollLoop, 0);
}

// Вернулись на вкладку — показываем свежие кэфы сразу, а не через паузу
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) restartPolling();
});

loadState();
els.viewTabs.querySelectorAll("button").forEach(
  (b) => b.classList.toggle("active", b.dataset.view === state.view));
updateVisibility();

restartPolling();
