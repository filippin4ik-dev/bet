/* Страница входа: единственная задача — обменять логин и пароль на cookie
 * сессии. После успеха возвращаемся туда, куда посетитель шёл (?next=…). */
"use strict";

const form = document.getElementById("gate-form");
const username = document.getElementById("gate-username");
const password = document.getElementById("gate-password");
const submit = document.getElementById("gate-submit");
const error = document.getElementById("gate-error");

function nextUrl() {
  const raw = new URLSearchParams(location.search).get("next") || "/";
  // только относительные адреса: с ?next=https://… страница входа стала бы
  // удобным трамплином для чужих ссылок
  return raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  error.hidden = true;
  submit.disabled = true;
  submit.textContent = "Проверяем…";
  try {
    const resp = await fetch("/api/access/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
      }),
    });
    if (!resp.ok) {
      let detail = "Неверный логин или пароль";
      try {
        detail = (await resp.json()).detail || detail;
      } catch (_) { /* тело не JSON — оставим текст по умолчанию */ }
      throw new Error(detail);
    }
    location.replace(nextUrl());
  } catch (err) {
    error.textContent = err.message || "Не удалось войти";
    error.hidden = false;
    password.select();
  } finally {
    submit.disabled = false;
    submit.textContent = "Войти";
  }
});
