/* Тема оформления: тёмная (по умолчанию) или светлая.
 *
 * Скрипт подключается в <head> ДО отрисовки: иначе при загрузке светлой
 * темы страница успевала мигнуть тёмной. Выбор запоминается в localStorage,
 * кнопка с id="theme-toggle" (если она есть на странице) переключает. */
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
  });
})();
