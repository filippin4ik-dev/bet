/* Валюта отображения: один пересчёт для всех страниц сайта.
 *
 * Движок считает и присылает деньги ТОЛЬКО в рублях — суммы плеч, лимит
 * ставки, баланс игрока и его журнал ставок хранятся и приходят в базовой
 * валюте. Валюта ОТОБРАЖЕНИЯ выбирается администратором (раздел «Валюта и
 * курс»), приходит с сервера вместе с данными и применяется здесь:
 *
 * - money.perRub — сколько единиц валюты в одном рубле (курс);
 * - toDisplay/toBaseRub — пересчёт туда и обратно. Обратно нужен всегда,
 *   когда сумма УХОДИТ на сервер: он запишет её в базу как рубли, и
 *   отправленные доллары уменьшили бы баланс почти в сто раз;
 * - все элементы с классом cur-sign получают знак валюты, поэтому
 *   подписи столбцов и полей не приходится трогать руками.
 *
 * Курса нет — сервер и валюту отдаёт рублёвую (см. app/currency.py),
 * поэтому здесь достаточно множителя 1: пересчитывать нечего.
 */
"use strict";

let money = {
  currency: "RUB",
  symbol: "₽",
  perRub: 1,
  banks: [1000, 5000, 10000],
  step: 100,
};

/* Вызывается после смены валюты: страница обновляет то, что зависит от
 * набора банков (см. app.js). */
let onCurrencyChange = null;

function toDisplay(rub) {
  if (rub === null || rub === undefined || !money.perRub) return null;
  return rub * money.perRub;
}

function toBaseRub(amount) {
  if (amount === null || amount === undefined || !money.perRub) return null;
  return Math.round((amount / money.perRub) * 100) / 100;
}

/* До какого знака округлять суммы. В рублях — до рубля: копейки в купоне
 * никому не нужны, а лишние два знака в двенадцати столбцах — мусор. В
 * долларах доллар «весит» почти сто рублей, и округление до целого
 * перекосило бы разбивку небольшого банка ($10 превратились бы в $4 и $6
 * вместо $3.62 и $6.38, а показанная прибыль стала бы неправдой). */
function moneyDecimals() {
  return money.perRub >= 1 ? 0 : 2;
}

function roundMoney(n) {
  const p = 10 ** moneyDecimals();
  return Math.round(Number(n) * p) / p;
}

function _fmt(n) {
  const d = moneyDecimals();
  return Number(n).toLocaleString("ru-RU",
    { minimumFractionDigits: d, maximumFractionDigits: d });
}

// Сумма, уже приведённая к валюте отображения.
const fmtMoney = (n) => n === null || n === undefined
  ? "—" : _fmt(n) + " " + money.symbol;

// Сумма, пришедшая с сервера в рублях (баланс игрока, лимит ставки).
const fmtRub = (n) => fmtMoney(toDisplay(n));

/* Число без знака валюты: в таблицах валюта стоит в заголовке столбца
 * («Ставка 1, ₽») — 12 столбцов и без повторяющегося знака еле влезают
 * в ноутбучный экран. */
const fmtNum = (n) => n === null || n === undefined ? "—" : _fmt(n);

const fmtNumRub = (n) => fmtNum(toDisplay(n));

function applyCurrency(state) {
  if (!state) return;
  const changed = state.currency !== money.currency
    || (state.per_rub || 1) !== money.perRub;
  money = {
    currency: state.currency,
    symbol: state.symbol,
    perRub: state.per_rub || 1,
    banks: state.banks && state.banks.length ? state.banks : money.banks,
    step: state.step || money.step,
  };
  for (const el of document.querySelectorAll(".cur-sign")) {
    el.textContent = money.symbol;
  }
  if (changed && onCurrencyChange) onCurrencyChange();
}

/* Валюта отдельной ручкой — для страниц, которые не тянут вилки
 * (профиль игрока). */
async function loadCurrency() {
  try {
    const resp = await fetch("/api/currency", { credentials: "same-origin" });
    if (resp.ok) applyCurrency(await resp.json());
  } catch (_) { /* нет связи — остаёмся в рублях */ }
}
