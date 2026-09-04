"use strict";

const els = {
  loginView: document.getElementById("login-view"),
  adminView: document.getElementById("admin-view"),
  loginForm: document.getElementById("login-form"),
  loginUsername: document.getElementById("login-username"),
  loginPassword: document.getElementById("login-password"),
  loginError: document.getElementById("login-error"),
  whoami: document.getElementById("whoami"),
  logoutBtn: document.getElementById("logout-btn"),
  accountsBody: document.getElementById("accounts-body"),
  addAccountBtn: document.getElementById("add-account-btn"),
  accountModal: document.getElementById("account-modal"),
  accountForm: document.getElementById("account-form"),
  accountBookmaker: document.getElementById("account-bookmaker"),
  accountLabel: document.getElementById("account-label"),
  accountLoginType: document.getElementById("account-login-type"),
  accountLoginLabel: document.getElementById("account-login-label"),
  accountLogin: document.getElementById("account-login"),
  accountPassword: document.getElementById("account-password"),
  accountCookies: document.getElementById("account-cookies"),
  accountCancel: document.getElementById("account-cancel"),
  accountError: document.getElementById("account-error"),
  settingEnabled: document.getElementById("setting-enabled"),
  settingDryRun: document.getElementById("setting-dry-run"),
  saveSettingsBtn: document.getElementById("save-settings-btn"),
  settingLiveEnabled: document.getElementById("setting-live-enabled"),
  saveScannerBtn: document.getElementById("save-scanner-btn"),
  scannerState: document.getElementById("scanner-state"),
  restartPrematchBtn: document.getElementById("restart-prematch-btn"),
  restartLiveBtn: document.getElementById("restart-live-btn"),
  restartAppBtn: document.getElementById("restart-app-btn"),
  parserGrid: document.getElementById("parser-grid"),
  parsersNote: document.getElementById("parsers-note"),
  accessState: document.getElementById("access-state"),
  accessPassword: document.getElementById("access-password"),
  accessWhitelist: document.getElementById("access-whitelist"),
  accessBlacklist: document.getElementById("access-blacklist"),
  currencyNote: document.getElementById("currency-note"),
  currencyChoice: document.getElementById("currency-choice"),
  currencyRate: document.getElementById("currency-rate"),
  currencyRateNote: document.getElementById("currency-rate-note"),
  currencyError: document.getElementById("currency-error"),
  saveCurrencyBtn: document.getElementById("save-currency-btn"),
  cbrRateBtn: document.getElementById("cbr-rate-btn"),
  accessIpOnly: document.getElementById("access-ip-only"),
  saveAccessBtn: document.getElementById("save-access-btn"),
  clearAccessBtn: document.getElementById("clear-access-btn"),
  addMyIpBtn: document.getElementById("add-my-ip-btn"),
  accessError: document.getElementById("access-error"),
  limitFraction: document.getElementById("limit-fraction"),
  limitMax: document.getElementById("limit-max"),
  betlogBody: document.getElementById("betlog-body"),
  panelAutobet: document.getElementById("panel-autobet"),
  panelBetlog: document.getElementById("panel-betlog"),
  navAutobet: document.getElementById("nav-autobet"),
  navBetlog: document.getElementById("nav-betlog"),
  playersBody: document.getElementById("players-body"),
  playersNote: document.getElementById("players-note"),
  playerForm: document.getElementById("player-form"),
  playerUsername: document.getElementById("player-username"),
  playerPassword: document.getElementById("player-password"),
  playerName: document.getElementById("player-name"),
  playerBalance: document.getElementById("player-balance"),
  playerError: document.getElementById("player-error"),
  toastHost: document.getElementById("toast-host"),
};

/* Короткие сообщения об успехе/ошибке: alert() останавливал бы опрос
 * состояния сканера и требовал клика на каждое действие. */
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`.trim();
  el.textContent = message;
  els.toastHost.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

async function api(path, opts) {
  const resp = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const j = await resp.json();
      detail = j.detail || detail;
    } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return resp.json();
}

function showLoggedIn(username) {
  els.loginView.hidden = true;
  els.adminView.hidden = false;
  els.whoami.textContent = `Вы: ${username}`;
  els.logoutBtn.hidden = false;
  sessionActive = true;
}

function showLoggedOut() {
  els.loginView.hidden = false;
  els.adminView.hidden = true;
  els.whoami.textContent = "";
  els.logoutBtn.hidden = true;
  sessionActive = false;
}

async function checkSession() {
  try {
    const me = await api("/api/admin/me");
    showLoggedIn(me.username);
    await loadAll();
  } catch (_) {
    showLoggedOut();
  }
}

els.loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.loginError.hidden = true;
  try {
    const res = await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({
        username: els.loginUsername.value,
        password: els.loginPassword.value,
      }),
    });
    showLoggedIn(res.username);
    await loadAll();
  } catch (err) {
    els.loginError.textContent = err.message || "Ошибка входа";
    els.loginError.hidden = false;
  }
});

els.logoutBtn.addEventListener("click", async () => {
  await api("/api/admin/logout", { method: "POST" });
  showLoggedOut();
});

/* Валюта отображения и курс: приходят вместе со списком аккаунтов и
 * из своего раздела админки (см. renderCurrency). */
let currencyState = null;

function fmtMoney(v, symbol = "₽") {
  if (v === null || v === undefined) return "—";
  return `${Number(v).toFixed(2)} ${symbol}`;
}

/* Баланс аккаунта — в валюте самой БК: крипто-площадка держит деньги в
 * долларах, и рублёвый знак завысил бы её баланс почти в сто раз. Рядом
 * показываем рублёвый эквивалент — именно он идёт в лимит ставки. */
function accountBalance(a) {
  const own = fmtMoney(a.balance, a.currency_symbol || "₽");
  if (a.currency === "RUB" || a.balance === null || a.balance === undefined) {
    return own;
  }
  const rub = a.balance_rub === null || a.balance_rub === undefined
    ? '<span class="warn-text" title="Курс доллара не задан в разделе «Валюта и курс» — этот баланс не учитывается в лимитах ставки">курс не задан</span>'
    : `<span class="muted">≈ ${Number(a.balance_rub).toFixed(2)} ₽</span>`;
  return `${own}<br>${rub}`;
}

function fmtDate(s) {
  if (!s) return "—";
  return s.replace("T", " ").replace(/\+00:00$/, " UTC");
}

/* Чем можно входить в каждую БК: {Winline: ["phone", "login"], …}.
 * Приходит с сервера, потому что знание живёт там же, где селекторы
 * формы входа — предлагать «по телефону» у БК, которая телефон не
 * принимает, значит подсунуть оператору форму, которая не отправится. */
let bkCatalog = { bookmakers: [], loginTypes: {}, names: {} };

async function loadBookmakers() {
  const data = await api("/api/admin/bookmakers");
  bkCatalog = {
    bookmakers: data.bookmakers || [],
    loginTypes: data.login_types || {},
    names: data.login_type_names || {},
  };
  return bkCatalog;
}

function loginTypeName(type) {
  return bkCatalog.names[type] || type;
}

/* Подпись поля реквизита — не «Логин/телефон/email» на все случаи, а то,
 * что реально просят ввести: иначе непонятно, в каком виде писать. */
function loginFieldLabel(type) {
  return type === "phone" ? "Телефон" : "Логин";
}

function loginFieldPlaceholder(type) {
  return type === "phone" ? "+7 912 345-67-89" : "логин или ID счёта";
}

function loginTypeSelect(account) {
  const allowed = bkCatalog.loginTypes[account.bookmaker] || ["phone", "login"];
  if (allowed.length < 2) {
    return `<span class="muted">${escapeHtml(loginTypeName(account.login_type))}</span>`;
  }
  const options = allowed.map((t) =>
    `<option value="${t}"${t === account.login_type ? " selected" : ""}>`
    + `${escapeHtml(loginTypeName(t))}</option>`).join("");
  return `<select class="acc-login-type" data-id="${account.id}"
      title="Способ входа: телефон и логин — разные вкладки формы у БК"
    >${options}</select>`;
}

async function loadAccounts() {
  const { accounts } = await api("/api/admin/accounts");
  els.accountsBody.innerHTML = accounts.length ? "" :
    '<tr><td colspan="10" class="muted">Аккаунтов нет. Они нужны только для '
    + 'лимитов ставки и авто-ставок — вилки считаются и без них.</td></tr>';
  for (const a of accounts) {
    const tr = document.createElement("tr");
    const status = a.last_error
      ? `<span class="err-text" title="${escapeHtml(a.last_error)}">ошибка</span>`
      : (a.balance !== null && a.balance !== undefined
        ? `<span class="ok-text">ок</span>` : "—");
    tr.innerHTML = `
      <td>${escapeHtml(a.bookmaker)}</td>
      <td>${escapeHtml(a.label || "")}</td>
      <td class="login-type-cell">${loginTypeSelect(a)}</td>
      <td>${escapeHtml(a.login)}</td>
      <td>${a.has_cookies ? '<span class="ok-text" title="Cookie задана — вход по ней, минуя форму/капчу">есть</span>' : "—"}</td>
      <td>${accountBalance(a)}</td>
      <td>${fmtDate(a.balance_updated_at)}</td>
      <td>${status}</td>
      <td><input type="checkbox" data-id="${a.id}" class="acc-enabled" ${a.enabled ? "checked" : ""}></td>
      <td>
        <button class="acc-refresh" data-id="${a.id}">Обновить баланс</button>
        <button class="acc-cookies" data-id="${a.id}">🍪 Cookie</button>
        <button class="acc-delete" data-id="${a.id}">Удалить</button>
      </td>`;
    els.accountsBody.appendChild(tr);
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

els.accountsBody.addEventListener("click", async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;
  if (e.target.classList.contains("acc-refresh")) {
    e.target.textContent = "...";
    try {
      await api(`/api/admin/accounts/${id}/refresh_balance`, { method: "POST" });
    } catch (err) { toast(err.message, "error"); }
    await loadAccounts();
  } else if (e.target.classList.contains("acc-delete")) {
    if (!confirm("Удалить аккаунт?")) return;
    await api(`/api/admin/accounts/${id}`, { method: "DELETE" });
    await loadAccounts();
  } else if (e.target.classList.contains("acc-cookies")) {
    const raw = window.prompt(
      "Вставьте cookie из СВОЕГО браузера (залогиненная сессия этой БК).\n" +
      "DevTools → Network → любой запрос к сайту БК → Headers → Request " +
      "Headers → Cookie — скопируйте всё значение.\n" +
      "Пустое значение — убрать сохранённую cookie (вернуться к обычному входу):"
    );
    if (raw === null) return;  // отмена
    try {
      await api(`/api/admin/accounts/${id}`, {
        method: "PUT",
        body: JSON.stringify({ cookies: raw }),
      });
    } catch (err) {
      toast(err.message || "Не удалось сохранить cookie", "error");
    }
    await loadAccounts();
  }
});

els.accountsBody.addEventListener("change", async (e) => {
  const id = e.target.dataset.id;
  if (!id) return;
  if (e.target.classList.contains("acc-enabled")) {
    await api(`/api/admin/accounts/${id}`, {
      method: "PUT",
      body: JSON.stringify({ enabled: e.target.checked }),
    });
  } else if (e.target.classList.contains("acc-login-type")) {
    try {
      await api(`/api/admin/accounts/${id}`, {
        method: "PUT",
        body: JSON.stringify({ login_type: e.target.value }),
      });
      toast(`Способ входа: ${loginTypeName(e.target.value)}. Обновляю баланс…`);
    } catch (err) {
      toast(err.message || "Не удалось сменить способ входа", "error");
    }
    await loadAccounts();
  }
});

/* Радиокнопки способа входа: показываем только те, что принимает
 * выбранная БК, и подстраиваем подпись поля реквизита. */
function syncLoginTypeChoice(bookmaker, preferred) {
  const allowed = bkCatalog.loginTypes[bookmaker] || ["phone", "login"];
  const options = els.accountLoginType.querySelectorAll("input[name='login-type']");
  let chosen = allowed.includes(preferred) ? preferred : allowed[0];
  for (const input of options) {
    const ok = allowed.includes(input.value);
    input.closest(".admin-choice__option").hidden = !ok;
    input.disabled = !ok;
    input.checked = input.value === chosen;
  }
  els.accountLoginType.hidden = allowed.length < 2;
  els.accountLoginLabel.textContent = loginFieldLabel(chosen);
  els.accountLogin.placeholder = loginFieldPlaceholder(chosen);
}

function chosenLoginType() {
  const checked = els.accountLoginType
    .querySelector("input[name='login-type']:checked");
  return checked ? checked.value : null;
}

els.accountBookmaker.addEventListener("change", () => {
  syncLoginTypeChoice(els.accountBookmaker.value, chosenLoginType());
});

els.accountLoginType.addEventListener("change", () => {
  const type = chosenLoginType();
  els.accountLoginLabel.textContent = loginFieldLabel(type);
  els.accountLogin.placeholder = loginFieldPlaceholder(type);
});

els.addAccountBtn.addEventListener("click", async () => {
  const { bookmakers } = await loadBookmakers();
  els.accountBookmaker.innerHTML = bookmakers
    .map((b) => `<option value="${b}">${b}</option>`).join("");
  els.accountLabel.value = "";
  els.accountLogin.value = "";
  els.accountPassword.value = "";
  els.accountCookies.value = "";
  syncLoginTypeChoice(els.accountBookmaker.value, null);
  els.accountError.hidden = true;
  els.accountModal.hidden = false;
});

els.accountCancel.addEventListener("click", () => {
  els.accountModal.hidden = true;
});

els.accountForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.accountError.hidden = true;
  try {
    await api("/api/admin/accounts", {
      method: "POST",
      body: JSON.stringify({
        bookmaker: els.accountBookmaker.value,
        label: els.accountLabel.value,
        login_type: chosenLoginType(),
        login: els.accountLogin.value,
        password: els.accountPassword.value,
        cookies: els.accountCookies.value,
      }),
    });
    els.accountModal.hidden = true;
    await loadAccounts();
  } catch (err) {
    els.accountError.textContent = err.message || "Не удалось добавить";
    els.accountError.hidden = false;
  }
});

/* Авто-ставки временно спрятаны от всех (AUTOBET_UI на сервере): разделы
 * «Авто-ставки» и «Журнал ставок» не показываем и не грузим. Механизм при
 * этом цел — вернуть интерфейс можно одной переменной окружения. */
let autobetUi = false;

function applyAutobetVisibility() {
  for (const el of [els.panelAutobet, els.panelBetlog,
                    els.navAutobet, els.navBetlog]) {
    if (el) el.hidden = !autobetUi;
  }
}

async function loadSettings() {
  const s = await api("/api/admin/settings");
  autobetUi = !!s.autobet_ui;
  applyAutobetVisibility();
  els.settingEnabled.checked = s.autobet_enabled;
  els.settingDryRun.checked = s.autobet_dry_run;
  els.limitFraction.textContent = `${Math.round(s.autobet_max_balance_fraction * 100)}%`;
  els.limitMax.textContent = s.autobet_max_stake;
  els.settingLiveEnabled.checked = s.live_enabled;
  renderScannerState(s);
}

/* ---------- игроки: учётные записи для входа на сайт ---------- */

function fmtRub(v) {
  return `${Math.round(Number(v || 0)).toLocaleString("ru-RU")} ₽`;
}

function renderPlayers(list) {
  const off = list.filter((p) => !p.enabled).length;
  els.playersNote.textContent = list.length
    ? `учёток: ${list.length}${off ? `, из них выключено ${off}` : ""}`
    : "ни одной учётки — войти можно только реквизитами администратора";
  els.playersNote.className = `panel-note ${list.length ? "ok" : "warn"}`;

  els.playersBody.innerHTML = list.length ? list.map((p) => `
    <tr class="${p.enabled ? "" : "row-blocked"}">
      <td><b>${escapeHtml(p.username)}</b></td>
      <td>${escapeHtml(p.display_name || "")}</td>
      <td>${fmtRub(p.start_balance)}</td>
      <td class="${p.profit >= 0 ? "ok-text" : "err-text"}">${p.profit >= 0 ? "+" : ""}${fmtRub(p.profit)}</td>
      <td><b>${fmtRub(p.balance)}</b></td>
      <td>${p.bets_count}</td>
      <td>${escapeHtml(fmtDate(p.last_login_at))}</td>
      <td><input type="checkbox" class="player-enabled" data-id="${p.id}"
                 ${p.enabled ? "checked" : ""}></td>
      <td>
        <button class="player-password" data-id="${p.id}"
                title="Задать новый пароль и продиктовать его игроку">Пароль</button>
        <button class="player-delete" data-id="${p.id}"
                data-name="${escapeHtml(p.username)}">Удалить</button>
      </td>
    </tr>`).join("")
    : '<tr><td colspan="9" class="muted">Игроков нет. Заведите учётку — '
      + 'без неё на сайт пускает только вход администратора.</td></tr>';
}

async function loadPlayers() {
  const { players } = await api("/api/admin/players");
  renderPlayers(players);
}

els.playerForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.playerError.hidden = true;
  try {
    await api("/api/admin/players", {
      method: "POST",
      body: JSON.stringify({
        username: els.playerUsername.value,
        password: els.playerPassword.value,
        display_name: els.playerName.value,
        start_balance: parseFloat(els.playerBalance.value) || 0,
      }),
    });
    toast(`Игрок ${els.playerUsername.value.trim()} заведён — передайте ему `
      + "логин и пароль");
    els.playerUsername.value = "";
    els.playerPassword.value = "";
    els.playerName.value = "";
    await loadPlayers();
  } catch (err) {
    els.playerError.textContent = err.message || "Не удалось завести игрока";
    els.playerError.hidden = false;
  }
});

els.playersBody.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-id]");
  if (!btn) return;
  const id = btn.dataset.id;
  try {
    if (btn.classList.contains("player-password")) {
      const password = window.prompt(
        "Новый пароль для игрока (продиктуйте его сами — обратно из базы "
        + "пароль не достать):");
      if (!password) return;
      await api(`/api/admin/players/${id}`, {
        method: "PUT",
        body: JSON.stringify({ password }),
      });
      toast("Пароль изменён");
    } else if (btn.classList.contains("player-delete")) {
      if (!confirm(`Удалить игрока ${btn.dataset.name} вместе с его журналом `
        + "ставок? Отменить это будет нечем.")) return;
      await api(`/api/admin/players/${id}`, { method: "DELETE" });
      toast("Игрок удалён");
    }
  } catch (err) {
    toast(err.message || "Не удалось изменить", "error");
  }
  await loadPlayers();
});

els.playersBody.addEventListener("change", async (e) => {
  const box = e.target.closest(".player-enabled");
  if (!box) return;
  try {
    await api(`/api/admin/players/${box.dataset.id}`, {
      method: "PUT",
      body: JSON.stringify({ enabled: box.checked }),
    });
    toast(box.checked ? "Игрок включён" : "Игрок выключен — войти он не сможет");
  } catch (err) {
    box.checked = !box.checked;
    toast(err.message || "Не удалось изменить", "error");
  }
  await loadPlayers();
});

function renderScannerState(s) {
  // Выключение вступает в силу не мгновенно: БК сначала докачивают то, что
  // уже начали, поэтому показываем и желаемое состояние, и фактическое.
  const parts = [`Прематч обновляет каждую БК раз в ${Math.round(s.scan_interval)} c.`];
  if (s.live_enabled) {
    parts.push(s.live_running ? "Лайв работает." : "Лайв запускается…");
  } else {
    parts.push(s.live_running
      ? "Лайв останавливается — БК докачивают текущие обходы…"
      : "Лайв не работает, все ресурсы прематчу.");
  }
  els.scannerState.textContent = parts.join(" ");
}

els.saveScannerBtn.addEventListener("click", async () => {
  els.saveScannerBtn.disabled = true;
  try {
    await api("/api/admin/settings", {
      method: "POST",
      body: JSON.stringify({ live_enabled: els.settingLiveEnabled.checked }),
    });
    await loadSettings();
  } finally {
    els.saveScannerBtn.disabled = false;
  }
});

els.saveSettingsBtn.addEventListener("click", async () => {
  try {
    await api("/api/admin/settings", {
      method: "POST",
      body: JSON.stringify({
        autobet_enabled: els.settingEnabled.checked,
        autobet_dry_run: els.settingDryRun.checked,
      }),
    });
    toast("Настройки авто-ставок сохранены");
  } catch (err) {
    toast(err.message || "Не удалось сохранить", "error");
  }
});

/* ---------- парсеры БК: включение, выключение, перезапуск ---------- */

function fmtAge(sec) {
  if (sec == null) return "";
  if (sec < 90) return "только что";
  return `${Math.round(sec / 60)} мин назад`;
}

function parserModeRow(label, info, offText) {
  if (offText) return `<div class="parser-row"><span class="mode">${label}</span>
      <span class="state">${offText}</span></div>`;
  if (!info) return `<div class="parser-row"><span class="mode">${label}</span>
      <span class="state">ждём первый обход</span></div>`;
  const state = info.busy ? '<span class="state busy">обновляется…</span>'
    : `<span class="state">${fmtAge(info.age_sec)}</span>`;
  /* Причину нуля показываем прямо здесь: иначе «0 котировок» одинаково
   * выглядит и когда БК не успела обновиться, и когда сайт неделю не
   * пускает сервер, а разница видна только в логах. */
  const why = info.note
    ? `<div class="parser-why">${escapeHtml(info.note)}</div>` : "";
  return `<div class="parser-row">
      <span class="mode">${label}</span>
      <span class="count">${info.count.toLocaleString("ru-RU")}</span>
      <span>котировок</span>${state}
    </div>${why}`;
}

/* Карточки БК живут между обновлениями: состояние подтягивается раз в
 * несколько секунд, и если каждый раз перерисовывать плитку целиком, клик
 * по галке, пришедшийся на этот момент, теряется вместе со старым узлом. */
const parserCards = new Map();
// БК, у которых переключение уже отправлено, но ответа ещё нет: их галку
// фоновое обновление не трогает, иначе она моргала бы в старое состояние
const parserPending = new Set();

function newParserCard(name) {
  const card = document.createElement("div");
  card.className = "parser-card";
  card.innerHTML = `
    <div class="parser-card-head">
      <span class="parser-name ${bkClass(name)}">${escapeHtml(name)}</span>
      <label class="switch" title="Включить или выключить эту БК">
        <input type="checkbox" class="parser-toggle" data-name="${escapeHtml(name)}">
        <span class="switch-state"></span>
      </label>
    </div>
    <div class="parser-rows"></div>
    <div class="parser-actions">
      <button class="soft-btn parser-restart" data-name="${escapeHtml(name)}">↻ Перезапустить</button>
    </div>`;
  return card;
}

function renderParsers(data) {
  els.parsersNote.textContent =
    `Прематч обновляет каждую БК раз в ${Math.round(data.scan_interval)} c` +
    (data.live_enabled ? ", лайв включён" : ", лайв выключен");
  for (const p of data.parsers) {
    let card = parserCards.get(p.name);
    if (!card) {
      card = newParserCard(p.name);
      parserCards.set(p.name, card);
      els.parserGrid.appendChild(card);
    }
    card.classList.toggle("off", !p.enabled);
    if (!parserPending.has(p.name)) {
      card.querySelector(".parser-toggle").checked = p.enabled;
      card.querySelector(".switch-state").textContent = p.enabled ? "вкл" : "выкл";
    }
    card.querySelector(".parser-rows").innerHTML =
      parserModeRow("Прематч", p.prematch, p.enabled ? "" : "БК выключена")
      + parserModeRow("Лайв", p.live,
        !p.supports_live ? "нет лайва у этой БК"
          : !p.enabled ? "БК выключена"
            : !data.live_enabled ? "лайв-сканер выключен" : "")
      + (p.min_refresh ? `<div class="parser-row"><span class="mode">Обход</span>
          <span>не чаще раза в ${Math.round(p.min_refresh)} c</span></div>` : "");
  }
  // БК могла уйти из набора (перезапуск сканера после смены настроек)
  const alive = new Set(data.parsers.map((p) => p.name));
  for (const [name, card] of parserCards) {
    if (!alive.has(name)) {
      card.remove();
      parserCards.delete(name);
    }
  }
}

async function loadParsers() {
  renderParsers(await api("/api/admin/parsers"));
}

const BK_CLASS = {
  "Fonbet": "bk-fonbet", "BetBoom": "bk-betboom", "Winline": "bk-winline",
  "Liga Stavok": "bk-liga", "Лига Ставок": "bk-liga", "bc.game": "bk-bcgame",
  "Roobet": "bk-roobet",
  "LeonBet": "bk-leon", "Betcity": "bk-betcity", "Melbet": "bk-melbet",
};
const bkClass = (bk) => BK_CLASS[bk] || "bk-other";

els.parserGrid.addEventListener("change", async (e) => {
  const box = e.target.closest(".parser-toggle");
  if (!box) return;
  const name = box.dataset.name;
  parserPending.add(name);
  try {
    await api(`/api/admin/parsers/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({ enabled: box.checked }),
    });
    toast(`${name}: ${box.checked ? "включена" : "выключена"}`);
  } catch (err) {
    box.checked = !box.checked;
    toast(err.message || "Не удалось изменить", "error");
  } finally {
    parserPending.delete(name);
  }
  await loadParsers();
});

els.parserGrid.addEventListener("click", async (e) => {
  const btn = e.target.closest(".parser-restart");
  if (!btn) return;
  const name = btn.dataset.name;
  btn.disabled = true;
  try {
    await api(`/api/admin/parsers/${encodeURIComponent(name)}/restart`,
              { method: "POST" });
    toast(`${name}: перезапуск — котировки соберутся заново`);
  } catch (err) {
    toast(err.message || "Не удалось перезапустить", "error");
  } finally {
    btn.disabled = false;
  }
});

async function restartScanner(mode, label) {
  try {
    await api("/api/admin/scanner/restart", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    toast(`${label}: перезапуск запущен (БК докачивают текущие обходы)`);
  } catch (err) {
    toast(err.message || "Не удалось перезапустить", "error");
  }
}

els.restartPrematchBtn.addEventListener("click",
  () => restartScanner("prematch", "Прематч"));
els.restartLiveBtn.addEventListener("click",
  () => restartScanner("live", "Лайв"));

els.restartAppBtn.addEventListener("click", async () => {
  if (!confirm("Перезапустить сервер? Сайт будет недоступен несколько секунд.")) return;
  els.restartAppBtn.disabled = true;
  try {
    const res = await api("/api/admin/restart_app", { method: "POST" });
    toast(res.detail || "Сервер перезапускается…", "warn");
    setTimeout(() => location.reload(), 6000);
  } catch (err) {
    toast(err.message || "Не удалось перезапустить сервер", "error");
    els.restartAppBtn.disabled = false;
  }
});

/* ---------- доступ к сайту: пароль и белый список IP ---------- */

let accessInfo = null;

function renderAccess(s) {
  accessInfo = s;
  els.accessWhitelist.value = (s.whitelist || []).join("\n");
  els.accessBlacklist.value = (s.ip_blacklist || []).join("\n");
  els.accessIpOnly.checked = !!s.ip_only;
  // Сайт закрыт всегда — состояние отвечает не «закрыт ли», а «чем
  // именно открывается»: учётки игроков, запасной пароль, ограничения по IP.
  const parts = [s.players_count
    ? `Учёток игроков: ${s.players_count}`
    : "Учёток игроков нет — пускает только вход администратора"];
  parts.push(s.password_set
    ? (s.password_source === "db" ? "запасной пароль задан в админке"
      : "запасной пароль из переменной SITE_PASSWORD")
    : "запасного пароля нет");
  if (s.whitelist.length) {
    parts.push(s.ip_only
      ? `строгий режим: только ${s.whitelist.length} адрес(ов) из списка`
      : `в списке ${s.whitelist.length} адрес(ов)`);
  }
  els.accessState.className = `panel-note ${s.players_count ? "ok" : "warn"}`;
  if (s.current_ip) parts.push(`ваш адрес ${s.current_ip}`);
  els.accessState.textContent = parts.join(" · ");
}

async function loadAccess() {
  renderAccess(await api("/api/admin/access"));
}

async function saveAccess(body, okText) {
  els.accessError.hidden = true;
  els.saveAccessBtn.disabled = true;
  try {
    renderAccess(await api("/api/admin/access", {
      method: "POST",
      body: JSON.stringify(body),
    }));
    els.accessPassword.value = "";
    toast(okText);
  } catch (err) {
    els.accessError.textContent = err.message || "Не удалось сохранить";
    els.accessError.hidden = false;
  } finally {
    els.saveAccessBtn.disabled = false;
  }
}

els.saveAccessBtn.addEventListener("click", () => saveAccess({
  password: els.accessPassword.value || null,
  whitelist: els.accessWhitelist.value,
  ip_blacklist: els.accessBlacklist.value,
  ip_only: els.accessIpOnly.checked,
}, "Настройки доступа сохранены"));

els.clearAccessBtn.addEventListener("click", () => {
  if (!confirm("Снять пароль? Сайт станет доступен без пароля всем, кроме "
    + "случая, когда включён строгий режим белого списка.")) return;
  saveAccess({ clear_password: true, ip_only: els.accessIpOnly.checked },
             "Пароль снят");
});

els.addMyIpBtn.addEventListener("click", () => {
  const ip = accessInfo && accessInfo.current_ip;
  if (!ip) return toast("Адрес не определён", "error");
  const lines = els.accessWhitelist.value.split(/\s+/).filter(Boolean);
  if (lines.some((l) => l === ip || l === `${ip}/32` || l === `${ip}/128`)) {
    return toast("Ваш адрес уже в списке");
  }
  lines.push(ip);
  els.accessWhitelist.value = lines.join("\n");
});

/* ---------- валюта отображения и курс доллара ---------- */

const RATE_SOURCE_NAMES = {
  cbr: "официальный курс ЦБ РФ",
  manual: "задан руками",
  env: "из переменной USD_RUB_RATE",
  none: "не задан",
};

function renderCurrency(state) {
  currencyState = state;
  const rate = state.usd_rub;
  // Доллар без курса выбрать нельзя: пересчитывать суммы нечем, а
  // выключенная кнопка объясняет это лучше ошибки после нажатия.
  els.currencyChoice.innerHTML = "<legend>Валюта отображения</legend>"
    + (state.currencies || []).map((c) => {
      const locked = c.code !== state.base && !rate;
      const title = locked
        ? "Сначала задайте курс доллара к рублю"
        : `Показывать суммы в ${c.name.toLowerCase()}`;
      return `<label class="admin-choice__option" title="${escapeHtml(title)}">
        <input type="radio" name="display-currency" value="${escapeHtml(c.code)}"
               ${c.code === state.currency ? "checked" : ""}
               ${locked ? "disabled" : ""}>
        <span>${escapeHtml(c.symbol)} ${escapeHtml(c.name)}</span>
      </label>`;
    }).join("");

  if (document.activeElement !== els.currencyRate) {
    els.currencyRate.value = rate == null ? "" : rate;
  }

  els.currencyNote.textContent = rate
    ? `${state.symbol} ${state.name} · $1 = ${rate} ₽`
    : `${state.symbol} ${state.name} · курс доллара не задан`;
  els.currencyNote.className = `panel-note ${rate ? "ok" : "warn"}`;

  const parts = [`Источник курса: ${RATE_SOURCE_NAMES[state.rate_source]
    || state.rate_source}`];
  if (state.rate_updated_at) {
    parts.push("обновлён "
      + new Date(state.rate_updated_at * 1000).toLocaleString("ru-RU"));
  }
  const crypto = state.crypto_bookmakers || [];
  if (crypto.length) {
    parts.push(rate
      ? `балансы крипто-БК (${crypto.join(", ")}) пересчитываются в рубли `
        + "этим курсом"
      : `⚠️ без курса балансы крипто-БК (${crypto.join(", ")}) в лимитах `
        + "ставки не учитываются");
  }
  els.currencyRateNote.textContent = parts.join(" · ");
}

async function loadCurrency() {
  renderCurrency(await api("/api/admin/currency"));
}

async function saveCurrency(body, okText) {
  els.currencyError.hidden = true;
  els.saveCurrencyBtn.disabled = true;
  els.cbrRateBtn.disabled = true;
  try {
    renderCurrency(await api("/api/admin/currency", {
      method: "POST",
      body: JSON.stringify(body),
    }));
    toast(okText);
    // Суммы в таблице аккаунтов подписаны валютой — перечитываем их,
    // чтобы знак и рублёвый эквивалент не остались от прошлого курса.
    await loadAccounts().catch(() => {});
  } catch (err) {
    els.currencyError.textContent = err.message || "Не удалось сохранить";
    els.currencyError.hidden = false;
    await loadCurrency().catch(() => {});
  } finally {
    els.saveCurrencyBtn.disabled = false;
    els.cbrRateBtn.disabled = false;
  }
}

function chosenCurrency() {
  const checked = els.currencyChoice
    .querySelector("input[name='display-currency']:checked");
  return checked ? checked.value : null;
}

els.saveCurrencyBtn.addEventListener("click", () => {
  const raw = els.currencyRate.value.trim();
  saveCurrency({
    currency: chosenCurrency(),
    usd_rub: raw === "" ? null : Number(raw.replace(",", ".")),
  }, "Валюта и курс сохранены");
});

els.cbrRateBtn.addEventListener("click", () => saveCurrency(
  { from_cbr: true }, "Курс обновлён с сайта ЦБ РФ"));

/* Валюту переключаем сразу по клику: отдельное «Сохранить» для одной
 * радиокнопки — лишний шаг, а курс рядом менять при этом не обязательно. */
els.currencyChoice.addEventListener("change", (e) => {
  if (e.target.name !== "display-currency") return;
  saveCurrency({ currency: e.target.value },
               `Суммы показываем в ${e.target.value}`);
});

async function loadBetLog() {
  if (!autobetUi) return;   // раздел спрятан — незачем и грузить
  const { log } = await api("/api/admin/bet_log?limit=100");
  els.betlogBody.innerHTML = log.length ? "" :
    '<tr><td colspan="7" class="muted">Ставок пока не было.</td></tr>';
  for (const entry of log) {
    const tr = document.createElement("tr");
    const legsText = entry.legs
      .map((l) => `${l.bookmaker}: ${l.stake}₽@${l.odds} ${l.ok ? "✓" : "✗ " + l.message}`)
      .join("; ");
    tr.innerHTML = `
      <td>${fmtDate(entry.ts)}</td>
      <td>${escapeHtml(entry.match || "")}</td>
      <td>${escapeHtml(entry.market || "")}</td>
      <td>${entry.profit_pct ?? "—"}%</td>
      <td>${entry.dry_run ? "имитация" : "реально"}</td>
      <td>${entry.ok ? '<span class="ok-text">успех</span>' : '<span class="err-text">ошибка</span>'}</td>
      <td title="${escapeHtml(legsText)}">${escapeHtml(legsText.slice(0, 60))}…</td>`;
    els.betlogBody.appendChild(tr);
  }
}

async function loadAll() {
  // Справочник БК — раньше аккаунтов: в их таблице стоит выбор способа
  // входа, а какие способы БК принимает, знает только справочник. Не
  // сложился — это не повод не показать админку: без него выбор просто
  // предложит оба способа вместо списка конкретной БК.
  await loadBookmakers().catch(() => {});
  // Настройки — раньше журнала ставок: по ним видно, показывать ли раздел
  // авто-ставок вообще (сейчас он спрятан, см. applyAutobetVisibility).
  await loadSettings();
  await Promise.all([loadAccounts(), loadPlayers(), loadParsers(),
                     loadAccess(), loadCurrency(), loadBetLog()]);
}

/* ---------- ретрансляция СМС/OTP-кода при входе ---------- */
/* Если БК запросила код при входе, коннектор ждёт его в фоновом потоке
 * (см. app/otp.py). Раз в несколько секунд проверяем, не ждёт ли
 * какой-то аккаунт код, и показываем диалог для его ввода. */

const otpPrompted = new Set();
let sessionActive = false;

async function pollOtp() {
  if (!sessionActive) return;
  try {
    const { pending } = await api("/api/admin/otp_pending");
    for (const p of pending) {
      if (otpPrompted.has(p.account_id)) continue;
      otpPrompted.add(p.account_id);
      const code = window.prompt(
        `БК «${p.bookmaker}» запросила код подтверждения (СМС/пуш) при входе.\n` +
        `Введите код — бот впишет его на сайте автоматически:`);
      if (code) {
        try {
          await api(`/api/admin/accounts/${p.account_id}/otp`, {
            method: "POST",
            body: JSON.stringify({ code }),
          });
        } catch (err) {
          toast(`Не удалось передать код: ${err.message}`, "error");
        }
      }
      otpPrompted.delete(p.account_id);
    }
  } catch (_) {
    // не залогинены / нет прав — просто не показываем диалог
  }
}
setInterval(pollOtp, 4000);

// Состояние сканера и БК меняется само (лайв останавливается не мгновенно —
// БК сначала докачивают начатые обходы), поэтому подтягиваем его в фоне.
// Плитки парсеров при этом показывают живую свежесть котировок.
setInterval(async () => {
  if (!sessionActive) return;
  if (document.hidden) return;   // вкладка в фоне — незачем дёргать сервер
  try {
    renderScannerState(await api("/api/admin/settings"));
    await loadParsers();
  } catch (_) { /* не залогинены — покажем при следующем входе */ }
}, 5000);

checkSession();
