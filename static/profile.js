/* Профиль игрока: стартовый баланс, прибыль и журнал сохранённых ставок.
 *
 * Баланс здесь нигде не хранится — он приходит с сервера как «стартовый
 * плюс прибыль журнала», поэтому после сохранения настроек или удаления
 * ставки достаточно перерисовать то, что ответила ручка. */
"use strict";

const els = {
  title: document.getElementById("profile-title"),
  login: document.getElementById("profile-login"),
  noPlayer: document.getElementById("no-player"),
  playerView: document.getElementById("player-view"),
  moneyStart: document.getElementById("money-start"),
  moneyProfit: document.getElementById("money-profit"),
  moneyBalance: document.getElementById("money-balance"),
  moneyStaked: document.getElementById("money-staked"),
  moneyBets: document.getElementById("money-bets"),
  form: document.getElementById("profile-form"),
  fieldName: document.getElementById("field-name"),
  fieldBalance: document.getElementById("field-balance"),
  fieldPassword: document.getElementById("field-password"),
  saveBtn: document.getElementById("save-profile"),
  error: document.getElementById("profile-error"),
  betsBody: document.getElementById("bets-body"),
  betsNote: document.getElementById("bets-note"),
  logoutBtn: document.getElementById("logout-btn"),
  toastHost: document.getElementById("toast-host"),
};

const fmtMoney = (n) => Math.round(Number(n)).toLocaleString("ru-RU") + " ₽";
const fmtNum = (n) => Math.round(Number(n)).toLocaleString("ru-RU");

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`.trim();
  el.textContent = message;
  els.toastHost.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

async function api(path, opts) {
  const resp = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.detail || resp.statusText);
    err.status = resp.status;   // 401 разбираем отдельно: сессия истекла
    throw err;
  }
  return data;
}

function renderPlayer(player) {
  els.title.textContent = player.name;
  els.login.textContent = player.username;
  els.moneyStart.textContent = fmtMoney(player.start_balance);
  els.moneyProfit.textContent = (player.profit >= 0 ? "+" : "") +
    fmtMoney(player.profit);
  els.moneyBalance.textContent = fmtMoney(player.balance);
  els.moneyStaked.textContent = fmtMoney(player.staked);
  els.moneyBets.textContent = `сохранённых ставок: ${player.bets_count}`;
  // Поля формы не перетираем, пока человек в них печатает: страница
  // перерисовывается после каждого сохранения и удаления записи.
  if (document.activeElement !== els.fieldName) {
    els.fieldName.value = player.display_name || "";
  }
  if (document.activeElement !== els.fieldBalance) {
    els.fieldBalance.value = player.start_balance;
  }
}

/* Дата хранится в UTC (ISO), показываем местным временем: журнал ведут,
 * чтобы сверяться с историей ставок в кабинете БК. */
function fmtWhen(iso) {
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z");
  if (isNaN(d)) return iso;
  return d.toLocaleString("ru-RU",
    { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function legsCell(legs) {
  return (legs || []).map((l) =>
    `<div class="bet-leg-line">
       <span class="out">${escapeHtml(l.outcome)}</span>
       <span class="coef">${Number(l.odds).toFixed(2)}</span>
       <span class="bk-chip">${escapeHtml(l.bookmaker)}</span>
       <span class="leg-stake">${fmtNum(l.stake)} ₽</span>
     </div>`).join("");
}

function renderBets(bets) {
  els.betsNote.textContent = bets.length
    ? `записей: ${bets.length}`
    : "";
  if (!bets.length) {
    els.betsBody.innerHTML = '<tr><td colspan="8" class="empty">Пока пусто. ' +
      'Поставьте вилку и нажмите «Сохранить ставку» в её карточке.</td></tr>';
    return;
  }
  els.betsBody.innerHTML = bets.map((b) => `<tr>
      <td data-label="Когда">${escapeHtml(fmtWhen(b.saved_at))}
        ${b.kind === "live" ? '<span class="kind live-cell">LIVE</span>' : ""}</td>
      <td data-label="Матч" class="match-name">${escapeHtml(b.match)}
        <span class="sport-league">${escapeHtml(b.sport)}</span></td>
      <td data-label="Рынок">${escapeHtml(b.market)}</td>
      <td data-label="Плечи">${legsCell(b.legs)}</td>
      <td data-label="Сумма" class="stake">${fmtNum(b.stake_total)}</td>
      <td data-label="Выигрыш" class="stake">${fmtNum(b.payout)}</td>
      <td data-label="Прибыль" class="stake profit-cell">${b.profit >= 0 ? "+" : ""}${fmtNum(b.profit)}</td>
      <td><button type="button" class="bet-del" data-id="${b.id}"
                  title="Удалить запись — баланс пересчитается">✕</button></td>
    </tr>`).join("");
}

async function load() {
  let me;
  try {
    me = await api("/api/profile/me");
  } catch (err) {
    // 401 тут — это шлюз сайта: сессии нет вообще, надо войти заново
    if (err.status === 401) {
      location.replace("/login?next=%2Fprofile");
      return;
    }
    toast(err.message || "Не удалось загрузить профиль", "error");
    return;
  }
  if (!me.player) {
    // вошли администратором или по общему паролю — своего журнала нет
    els.noPlayer.hidden = false;
    els.playerView.hidden = true;
    return;
  }
  els.noPlayer.hidden = true;
  els.playerView.hidden = false;
  renderPlayer(me.player);
  try {
    const data = await api("/api/profile/bets");
    renderPlayer(data.player);
    renderBets(data.bets);
  } catch (err) {
    toast(err.message || "Не удалось загрузить журнал ставок", "error");
  }
}

els.form.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.error.hidden = true;
  els.saveBtn.disabled = true;
  try {
    const data = await api("/api/profile", {
      method: "POST",
      body: JSON.stringify({
        display_name: els.fieldName.value,
        start_balance: parseFloat(els.fieldBalance.value) || 0,
        password: els.fieldPassword.value || null,
      }),
    });
    els.fieldPassword.value = "";
    renderPlayer(data.player);
    toast("Профиль сохранён");
  } catch (err) {
    els.error.textContent = err.message || "Не удалось сохранить";
    els.error.hidden = false;
  } finally {
    els.saveBtn.disabled = false;
  }
});

els.betsBody.addEventListener("click", async (e) => {
  const btn = e.target.closest("button.bet-del");
  if (!btn) return;
  if (!confirm("Удалить запись? Баланс уменьшится на её прибыль.")) return;
  try {
    await api(`/api/profile/bets/${btn.dataset.id}`, { method: "DELETE" });
    toast("Запись удалена");
  } catch (err) {
    toast(err.message || "Не удалось удалить", "error");
  }
  await load();
});

els.logoutBtn.addEventListener("click", async () => {
  try {
    await fetch("/api/access/logout", {
      method: "POST", credentials: "same-origin" });
  } catch (_) { /* уводим на форму входа в любом случае */ }
  location.replace("/login");
});

load();
