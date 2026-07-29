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
  limitFraction: document.getElementById("limit-fraction"),
  limitMax: document.getElementById("limit-max"),
  betlogBody: document.getElementById("betlog-body"),
};

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

async function loadAccounts() {
  const { accounts } = await api("/api/admin/accounts");
  els.accountsBody.innerHTML = "";
  for (const a of accounts) {
    const tr = document.createElement("tr");
    const status = a.last_error
      ? `<span class="err-text" title="${escapeHtml(a.last_error)}">ошибка</span>`
      : (a.balance !== null && a.balance !== undefined
        ? `<span class="ok-text">ок</span>` : "—");
    tr.innerHTML = `
      <td>${escapeHtml(a.bookmaker)}</td>
      <td>${escapeHtml(a.label || "")}</td>
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
    } catch (err) { alert(err.message); }
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
      alert(err.message || "Не удалось сохранить cookie");
    }
    await loadAccounts();
  }
});

els.accountsBody.addEventListener("change", async (e) => {
  if (!e.target.classList.contains("acc-enabled")) return;
  const id = e.target.dataset.id;
  await api(`/api/admin/accounts/${id}`, {
    method: "PUT",
    body: JSON.stringify({ enabled: e.target.checked }),
  });
});

els.addAccountBtn.addEventListener("click", async () => {
  const { bookmakers } = await api("/api/admin/bookmakers");
  els.accountBookmaker.innerHTML = bookmakers
    .map((b) => `<option value="${b}">${b}</option>`).join("");
  els.accountLabel.value = "";
  els.accountLogin.value = "";
  els.accountPassword.value = "";
  els.accountCookies.value = "";
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
  await api("/api/admin/settings", {
    method: "POST",
    body: JSON.stringify({
      autobet_enabled: els.settingEnabled.checked,
      autobet_dry_run: els.settingDryRun.checked,
    }),
  });
  alert("Сохранено");
});

async function loadBetLog() {
  const { log } = await api("/api/admin/bet_log?limit=100");
  els.betlogBody.innerHTML = "";
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
  await Promise.all([loadAccounts(), loadSettings(), loadBetLog()]);
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
          alert(`Не удалось передать код: ${err.message}`);
        }
      }
      otpPrompted.delete(p.account_id);
    }
  } catch (_) {
    // не залогинены / нет прав — просто не показываем диалог
  }
}
setInterval(pollOtp, 4000);

// Состояние сканера меняется само (лайв останавливается не мгновенно —
// БК сначала докачивают начатые обходы), поэтому подтягиваем его в фоне.
setInterval(async () => {
  if (!sessionActive) return;
  try {
    renderScannerState(await api("/api/admin/settings"));
  } catch (_) { /* не залогинены — покажем при следующем входе */ }
}, 5000);

checkSession();
