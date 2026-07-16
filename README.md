# Сканер вилок П1/П2

Веб-приложение для автоматического поиска **двухисходных вилок** (только П1 и П2, без ничьих, фор и тоталов) по четырём БК: **Winline, BetBoom, Fonbet, Liga Stavok**.

Сканер проверяет **все события линии** (Live и прематч) и **все подходящие двухисходные котировки**, включая дочерние рынки — победителя сета, партии, карты и т.п. Счётчик проверенных событий и котировок отображается прямо на сайте.

## Как это работает

1. Парсеры каждые 10 секунд собирают коэффициенты П1 и П2 со всех 4 БК (все виды спорта, все события; рынки с ничьей отбрасываются автоматически).
2. Для каждого события берутся максимальные кэфы среди БК: `К1_max`, `К2_max` (с указанием, какая БК их дала).
3. Вилка: `1/К1_max + 1/К2_max < 1`.
4. Для найденных вилок считается доходность в % и суммы ставок при банке 1 000 / 5 000 / 10 000 ₽ (выигрыш одинаковый при любом исходе).

Пример: `К1_max = 2.30 (BetBoom)`, `К2_max = 1.85 (Winline)` → `1/2.30 + 1/1.85 = 0.9753` → доходность **2.53 %**, при банке 1 000 ₽ ставки **446 ₽ / 554 ₽**.

## Возможности

- Таблица: тип (LIVE / прематч + время начала), спорт и рынок, матч, `К1_max` (с БК), `К2_max` (с БК), доходность %, суммы ставок и прибыль для выбранного банка.
- Переключатель рынков: **Все / Live / Прематч**.
- Фильтр по минимальной доходности (например, только > 2 %).
- Автообновление каждые 10 секунд.
- Звуковое уведомление при появлении новой вилки с доходностью > 2.5 % (порог настраивается).
- История всех найденных вилок в SQLite (`GET /api/history`).
- Защита от блокировок: ротация User-Agent, случайные задержки 2–5 секунд между запросами к одной БК, падение одной БК не останавливает сканер.

---

## Быстрый запуск на своём компьютере

Нужен Python 3.10 или новее.

```bash
git clone https://github.com/filippin4ik-dev/bet.git
cd bet
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Открыть в браузере: http://localhost:8000

По умолчанию приложение стартует в **демо-режиме** (встроенный симулятор котировок ~70–80 событий) — можно сразу увидеть, как всё работает. Реальный парсинг включается переменной `SCANNER_MODE=live` (см. ниже).

---

## Развёртывание на VPS (Ubuntu 22.04 / 24.04)

> Для live-режима берите VPS с **российским IP** — сайты БК доступны только из РФ.

### Шаг 1. Подключиться к серверу и поставить зависимости

```bash
ssh root@IP_ВАШЕГО_СЕРВЕРА

apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git nginx
```

### Шаг 2. Скачать проект и установить пакеты

```bash
mkdir -p /opt/arb-scanner
git clone https://github.com/filippin4ik-dev/bet.git /opt/arb-scanner
cd /opt/arb-scanner

python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### Шаг 3. Проверить, что запускается

```bash
cd /opt/arb-scanner
SCANNER_MODE=demo venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Открыть `http://IP_СЕРВЕРА:8000` — должна появиться таблица с вилками. Остановить: `Ctrl+C`.

### Шаг 4. Автозапуск через systemd

Готовый юнит лежит в `deploy/arb-scanner.service`. Установка:

```bash
chown -R www-data:www-data /opt/arb-scanner
cp deploy/arb-scanner.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now arb-scanner
```

Проверить состояние и логи:

```bash
systemctl status arb-scanner
journalctl -u arb-scanner -f
```

Режим (demo/live), период сканирования и прочее меняются в секции `Environment=` файла `/etc/systemd/system/arb-scanner.service`, после правки:

```bash
systemctl daemon-reload && systemctl restart arb-scanner
```

### Шаг 5. Nginx — доступ по 80-му порту

Готовый конфиг лежит в `deploy/nginx.conf`:

```bash
cp deploy/nginx.conf /etc/nginx/sites-available/arb-scanner
# впишите свой домен в server_name (или удалите строку, если доступ по IP)
nano /etc/nginx/sites-available/arb-scanner

ln -s /etc/nginx/sites-available/arb-scanner /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

Теперь сайт доступен по `http://IP_СЕРВЕРА` (без порта).

### Шаг 6. Файрвол (рекомендуется)

```bash
apt install -y ufw
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw enable
```

### Шаг 7 (по желанию). HTTPS с бесплатным сертификатом

Если есть домен, направленный на сервер:

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d ваш-домен.ru
```

### Обновление до свежей версии

```bash
cd /opt/arb-scanner
git pull
venv/bin/pip install -r requirements.txt
systemctl restart arb-scanner
```

---

## Настройки (переменные окружения)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SCANNER_MODE` | `demo` | `demo` — встроенный симулятор котировок; `live` — реальный парсинг 4 БК |
| `SCAN_INTERVAL` | `10` | Период обновления коэффициентов, сек |
| `SOUND_ALERT_PROFIT` | `2.5` | Порог доходности для звукового уведомления, % |
| `REQUEST_DELAY_MIN` / `REQUEST_DELAY_MAX` | `2` / `5` | Случайная задержка между запросами к одной БК, сек |
| `DB_PATH` | `arbs.sqlite3` | Файл SQLite с историей найденных вилок |

Пример live-запуска вручную:

```bash
SCANNER_MODE=live venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Важно про live-режим

- Сайты БК доступны **только с российских IP** и активно защищаются от парсинга; вёрстка периодически меняется.
- Парсер **Fonbet** работает через публичный JSON-API линии и забирает все события и все двухисходные рынки (включая сеты/периоды/карты).
- Для **Winline / BetBoom / Лиги Ставок** CSS-селекторы в `app/parsers/*.py` может потребоваться актуализировать под текущую вёрстку сайтов.
- Для динамических страниц раскомментируйте `selenium` и `webdriver-manager` в `requirements.txt` и поставьте Chromium: `apt install -y chromium-browser` (парсеры подхватят его автоматически как запасной вариант).

## API

| Метод | Описание |
|---|---|
| `GET /api/arbs?min_profit=2&kind=all` | Текущие вилки с доходностью ≥ 2 % (`kind`: `all` / `live` / `prematch`) |
| `GET /api/history?limit=100` | История найденных вилок из SQLite |

## Структура проекта

```
app/
  main.py          # FastAPI: API + раздача фронтенда
  scanner.py       # фоновый цикл сканирования (каждые 10 с)
  arbitrage.py     # формула вилки и расчёт сумм ставок
  db.py            # SQLite-история
  config.py        # настройки
  models.py        # модели данных
  parsers/
    base.py            # ротация UA, задержки, обработка ошибок
    winline.py         # Winline: live + прематч (HTML + BS4, запасной Selenium)
    betboom.py         # BetBoom: live + прематч (SPA — Selenium, запасной requests)
    fonbet.py          # Fonbet: JSON-API, все события и дочерние рынки
    ligastavok.py      # Лига Ставок: live + прематч (HTML + BS4)
    demo.py            # демо-симулятор полной линии (~70–80 событий)
    selenium_helper.py # опциональный JS-рендеринг
static/
  index.html, app.js, style.css  # фронтенд без фреймворков
deploy/
  arb-scanner.service  # systemd-юнит для VPS
  nginx.conf           # конфиг nginx (обратный прокси)
```

## Дисклеймер

Проект предназначен для образовательных целей. Букмекеры ограничивают счета «вилочников»; проверяйте законодательство вашей юрисдикции и правила БК.
