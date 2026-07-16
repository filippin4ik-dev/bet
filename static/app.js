/* Сканер вилок П1/П2 — фронтенд без фреймворков.
 * Опрашивает /api/arbs каждые 10 секунд, рисует таблицу,
 * фильтрует по доходности и подаёт звук при вилке > 2.5 %. */

const POLL_INTERVAL_MS = 10_000;

const els = {
  body: document.getElementById("arbs-body"),
  kindTabs: document.getElementById("kind-tabs"),
  minProfit: document.getElementById("min-profit"),
  bank: document.getElementById("bank"),
  soundOn: document.getElementById("sound-on"),
  soundThreshold: document.getElementById("sound-threshold"),
  testSound: document.getElementById("test-sound"),
  modeBadge: document.getElementById("mode-badge"),
  lastScan: document.getElementById("last-scan"),
  coverage: document.getElementById("coverage"),
  arbCount: document.getElementById("arb-count"),
  interval: document.getElementById("interval"),
};

let soundAlertProfit = 2.5;
let alertedKeys = new Set(); // вилки, о которых уже «пропищали»
let currentKind = "all";     // all | live | prematch

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

/* ---------- отрисовка ---------- */

const fmtMoney = (n) => Number(n).toLocaleString("ru-RU") + " ₽";

function kindCell(a) {
  if (a.kind === "prematch") {
    const when = a.start_time ? `<span class="bk">${a.start_time}</span>` : "";
    return `<span class="kind prematch">Прематч</span> ${when}`;
  }
  return '<span class="kind live">LIVE</span>';
}

function render(arbs) {
  const bank = els.bank.value;
  els.arbCount.textContent = `Вилок: ${arbs.length}`;

  if (!arbs.length) {
    els.body.innerHTML =
      '<tr><td colspan="10" class="empty">Вилок нет — ждём следующего обновления…</td></tr>';
    return;
  }

  els.body.innerHTML = arbs.map((a) => {
    const st = a.stakes[bank] || {};
    const hot = a.profit_pct > soundAlertProfit ? " class=\"hot\"" : "";
    return `<tr${hot}>
      <td>${kindCell(a)}</td>
      <td>${a.sport}</td>
      <td>${a.match}</td>
      <td>${a.market}</td>
      <td><span class="out">${a.outcome1}</span> <span class="coef">${a.k1_max.toFixed(2)}</span> <span class="bk">${a.k1_bookmaker}</span></td>
      <td><span class="out">${a.outcome2}</span> <span class="coef">${a.k2_max.toFixed(2)}</span> <span class="bk">${a.k2_bookmaker}</span></td>
      <td class="profit">${a.profit_pct.toFixed(2)} %</td>
      <td class="stake">${fmtMoney(st.stake1)} <span class="bk">${a.outcome1} · ${a.k1_bookmaker}</span></td>
      <td class="stake">${fmtMoney(st.stake2)} <span class="bk">${a.outcome2} · ${a.k2_bookmaker}</span></td>
      <td class="stake">+${fmtMoney(st.profit)}</td>
    </tr>`;
  }).join("");
}

/* ---------- опрос API ---------- */

async function poll() {
  try {
    const minProfit = parseFloat(els.minProfit.value) || 0;
    const resp = await fetch(`/api/arbs?min_profit=${minProfit}&kind=${currentKind}`);
    const data = await resp.json();

    soundAlertProfit = data.sound_alert_profit ?? 2.5;
    els.soundThreshold.textContent = soundAlertProfit;
    els.interval.textContent = data.scan_interval;

    els.modeBadge.textContent = data.mode === "live" ? "LIVE" : "ДЕМО";
    els.modeBadge.className = "badge " + data.mode;

    if (data.last_scan) {
      els.lastScan.textContent = "Обновлено: " +
        new Date(data.last_scan * 1000).toLocaleTimeString("ru-RU");
    }

    if (data.events_checked) {
      els.coverage.textContent =
        `Проверено: ${data.events_checked} событий / ${data.quotes_checked} котировок`;
    }

    render(data.arbs);

    // Звук — только для НОВЫХ вилок с доходностью выше порога
    const hot = data.arbs.filter((a) => a.profit_pct > soundAlertProfit);
    const fresh = hot.filter((a) => !alertedKeys.has(a.match_key));
    if (fresh.length && els.soundOn.checked) beep();
    alertedKeys = new Set(hot.map((a) => a.match_key));
  } catch (err) {
    els.lastScan.textContent = "Ошибка связи с сервером…";
  }
}

els.minProfit.addEventListener("change", poll);
els.bank.addEventListener("change", poll);

els.kindTabs.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-kind]");
  if (!btn) return;
  currentKind = btn.dataset.kind;
  els.kindTabs.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("active", b === btn));
  poll();
});

poll();
setInterval(poll, POLL_INTERVAL_MS);
