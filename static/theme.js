/* Тема оформления и обвязка липкой шапки.
 *
 * Скрипт подключается в <head> ДО отрисовки: иначе при загрузке светлой
 * темы страница успевала мигнуть тёмной. Выбор запоминается в localStorage,
 * кнопка с id="theme-toggle" (если она есть на странице) переключает.
 *
 * Здесь же меряется высота шапки: она липкая, и заголовки таблиц липнут
 * тоже — но к top: 0, то есть ровно ПОД шапку, так что при прокрутке
 * подписи столбцов пропадали. Высота шапки плавает (статусы и чипы БК
 * переносятся по-разному на каждой ширине), поэтому её приходится мерить
 * и класть в --header-h, от которой отталкивается CSS. */
(function () {
  "use strict";
  const KEY = "arb-scanner-theme";
  const root = document.documentElement;

  function apply(theme) {
    root.dataset.theme = theme;
  }

  // По умолчанию тёмная: за таблицей кэфов сидят часами, и системная
  // светлая тема (обычная у ноутбуков) здесь скорее мешает. Выбор
  // пользователя запоминается и важнее настройки системы.
  apply(localStorage.getItem(KEY) || "dark");

  document.addEventListener("DOMContentLoaded", function () {
    initToggle();
    initHeader();
  });

  function initToggle() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    function label() {
      const light = root.dataset.theme === "light";
      btn.textContent = light ? "🌙" : "☀️";
      btn.title = light ? "Тёмная тема" : "Светлая тема";
    }
    label();
    btn.addEventListener("click", function () {
      apply(root.dataset.theme === "light" ? "dark" : "light");
      localStorage.setItem(KEY, root.dataset.theme);
      label();
    });
  }

  function initHeader() {
    const header = document.querySelector("body > header");
    if (!header) return;

    // На телефоне шапка не липкая (см. style.css), и заголовкам таблиц
    // отступать не от чего — тогда --header-h должна быть нулём.
    function measure() {
      const stuck = getComputedStyle(header).position === "sticky";
      root.style.setProperty(
        "--header-h", (stuck ? Math.round(header.offsetHeight) : 0) + "px");
    }

    // Прокрутили — шапке нужна тень: без неё она сливается с уезжающей
    // под неё таблицей и не читается как отдельный слой.
    function scrolled() {
      root.dataset.scrolled = window.scrollY > 4 ? "1" : "0";
    }

    measure();
    scrolled();
    if (window.ResizeObserver) new ResizeObserver(measure).observe(header);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", scrolled, { passive: true });
  }
})();
