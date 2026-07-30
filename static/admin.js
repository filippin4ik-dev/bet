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
  visitorsNote: document.getElementById("visitors-note"),
  visitorsBody: document.getElementById("visitors-body"),
  bannedIps: document.getElementById("banned-ips"),
  accessIpOnly: document.getElementById("access-ip-only"),
  saveAccessBtn: document.getElementById("save-access-btn"),
  clearAccessBtn: document.getElementById("clear-access-btn"),
  addMyIpBtn: document.getElementById("add-my-ip-btn"),
  accessError: document.getElementById("access-error"),
  limitFraction: document.getElementById("limit-fraction"),
  limitMax: document.getElementById("limit-max"),
  betlogBody: document.getElementById("betlog-body"),
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

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  return `${Number(v).toFixed(2)} ₽`;
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
      <td>${fmtMoney(a.balance)}</td>
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

async function loadSettings() {
  const s = await api("/api/admin/settings");
  els.settingEnabled.checked = s.autobet_enabled;
  els.settingDryRun.checked = s.autobet_dry_run;
  els.limitFraction.textContent = `${Math.round(s.autobet_max_balance_fraction * 100)}%`;
  els.limitMax.textContent = s.autobet_max_stake;
  els.settingLiveEnabled.checked = s.live_enabled;
  renderScannerState(s);
}

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
  return `<div class="parser-row">
      <span class="mode">${label}</span>
      <span class="count">${info.count.toLocaleString("ru-RU")}</span>
      <span>котировок</span>${state}
    </div>`;
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
  const parts = [];
  if (!s.gate_enabled) {
    parts.push("Сайт открыт всем, кто знает адрес сервера");
    els.accessState.className = "panel-note warn";
  } else {
    parts.push(s.password_set
      ? (s.password_source === "db" ? "Пароль задан в админке"
        : "Пароль из переменной SITE_PASSWORD")
      : "Пароль не задан");
    if (s.whitelist.length) {
      parts.push(s.ip_only
        ? `только ${s.whitelist.length} адрес(ов) из списка`
        : `${s.whitelist.length} адрес(ов) входят без пароля`);
    }
    els.accessState.className = "panel-note ok";
  }
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

/* ---------- кто на сайте: устройства, адреса, бан ---------- */

let visitorsInfo = null;

function fmtSince(sec) {
  if (sec == null) return "—";
  if (sec < 60) return "только что";
  if (sec < 3600) return `${Math.round(sec / 60)} мин назад`;
  if (sec < 86400) return `${Math.round(sec / 3600)} ч назад`;
  return `${Math.round(sec / 86400)} сут назад`;
}

function visitorTags(v, data) {
  const tags = [`<span class="tag">${escapeHtml(v.kind)}</span>`];
  if (v.device_id === data.my_device) tags.push('<span class="tag me">это вы</span>');
  if (v.admin) tags.push('<span class="tag admin">админка</span>');
  // «по паролю» у админского устройства не пишем: сессия админки открывает
  // сайт сама, и пометка только сбивала бы с толку
  else if (v.authed) tags.push('<span class="tag" title="Вошёл по паролю доступа">по паролю</span>');
  if (v.blocked) {
    tags.push(v.block_reason === "twin"
      ? '<span class="tag banned" title="Вернулось тем же браузером с того же адреса, но уже без cookie">забанен (вернулся)</span>'
      : '<span class="tag banned">забанен</span>');
  }
  return tags.join(" ");
}

function renderVisitors(data) {
  visitorsInfo = data;
  const list = data.visitors || [];
  const online = list.filter((v) => v.online).length;
  els.visitorsNote.textContent = !data.tracking
    ? "Учёт посетителей выключен (VISITORS_ENABLED=0)"
    : `${online} сейчас на сайте · всего устройств: ${list.length}`;
  els.visitorsNote.className = `panel-note ${online ? "ok" : ""}`.trim();

  els.visitorsBody.innerHTML = list.length ? list.map((v) => {
    const ips = (v.ips || []).join(", ");
    const banned = (data.ip_blacklist || []).some(
      (e) => e === `${v.ip}/32` || e === `${v.ip}/128` || e === v.ip);
    const actions = v.blocked
      ? `<button class="vis-unblock" data-device="${escapeHtml(v.device_id)}">Разбанить</button>`
      : `<button class="vis-block" data-device="${escapeHtml(v.device_id)}"
                 title="Закрыть сайт для этого устройства">Забанить</button>
         <button class="vis-block-ip" data-device="${escapeHtml(v.device_id)}"
                 title="Забанить устройство и его текущий адрес">+ адрес</button>`;
    return `<tr class="${v.blocked ? "row-blocked" : ""}">
      <td data-label="Устройство" class="vis-device">
        <span class="dev-dot ${v.online ? "online" : ""}"></span>
        <b>${escapeHtml(v.browser)}</b>${v.os ? " · " + escapeHtml(v.os) : ""}
        ${visitorTags(v, data)}
      </td>
      <td data-label="Адрес" title="${escapeHtml(ips)}">
        ${escapeHtml(v.ip || "—")}${banned ? ' <span class="tag banned">IP забанен</span>' : ""}
      </td>
      <td data-label="Активность">${v.online
        ? '<span class="ok-text">сейчас на сайте</span>'
        : escapeHtml(fmtSince(v.age_sec))}</td>
      <td data-label="Запросов">${v.hits.toLocaleString("ru-RU")}</td>
      <td data-label="Впервые">${escapeHtml(fmtSince(v.seen_sec))}</td>
      <td class="vis-actions">${actions}
        <button class="vis-forget" data-device="${escapeHtml(v.device_id)}"
                title="Убрать из списка; при следующем заходе появится заново">Забыть</button>
      </td>
    </tr>`;
  }).join("") : '<tr><td colspan="6" class="muted">Пока никто не заходил.</td></tr>';

  const bannedIps = data.ip_blacklist || [];
  els.bannedIps.hidden = !bannedIps.length;
  els.bannedIps.innerHTML = bannedIps.length
    ? "Забаненные адреса: " + bannedIps.map((e) =>
      `<span class="ip-chip">${escapeHtml(e)}
        <button class="ip-unban" data-ip="${escapeHtml(e)}" title="Разбанить">×</button>
      </span>`).join(" ")
    : "";
}

async function loadVisitors() {
  renderVisitors(await api("/api/admin/visitors"));
}

els.visitorsBody.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-device]");
  if (!btn) return;
  const device = btn.dataset.device;
  const cls = btn.className;
  try {
    if (cls === "vis-forget") {
      if (!confirm("Убрать устройство из списка? Бан с него снимется.")) return;
      await api(`/api/admin/visitors/${encodeURIComponent(device)}`,
                { method: "DELETE" });
      toast("Устройство забыто");
    } else if (cls === "vis-unblock") {
      await api(`/api/admin/visitors/${encodeURIComponent(device)}/block`, {
        method: "POST",
        body: JSON.stringify({ blocked: false }),
      });
      toast("Устройство разблокировано (адрес, если банили, остался в списке)");
    } else {
      const withIp = cls === "vis-block-ip";
      if (!confirm(withIp
        ? "Забанить устройство и его текущий адрес?"
        : "Закрыть сайт для этого устройства?")) return;
      await api(`/api/admin/visitors/${encodeURIComponent(device)}/block`, {
        method: "POST",
        body: JSON.stringify({ blocked: true, with_ip: withIp }),
      });
      toast("Устройство забанено");
    }
  } catch (err) {
    toast(err.message || "Не удалось изменить", "error");
  }
  await Promise.all([loadVisitors(), loadAccess()]);
});

els.bannedIps.addEventListener("click", async (e) => {
  const btn = e.target.closest(".ip-unban");
  if (!btn) return;
  const rest = (visitorsInfo.ip_blacklist || [])
    .filter((entry) => entry !== btn.dataset.ip);
  try {
    await api("/api/admin/access", {
      method: "POST",
      body: JSON.stringify({ ip_blacklist: rest.join("\n") }),
    });
    toast(`Адрес ${btn.dataset.ip} разбанен`);
  } catch (err) {
    toast(err.message || "Не удалось разбанить", "error");
  }
  await Promise.all([loadVisitors(), loadAccess()]);
});

async function loadBetLog() {
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
  // входа, а какие способы БК принимает, знает только справочник.
  await loadBookmakers();
  await Promise.all([loadAccounts(), loadSettings(), loadParsers(),
                     loadAccess(), loadVisitors(), loadBetLog()]);
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
    await loadVisitors();
  } catch (_) { /* не залогинены — покажем при следующем входе */ }
}, 5000);

checkSession();
