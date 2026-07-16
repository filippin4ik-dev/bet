"""Настройки приложения (переопределяются переменными окружения)."""
import os

# Сканер всегда работает только с реальными данными БК
# (Winline / BetBoom / Fonbet / Liga Stavok). Демо-режима нет.
SCANNER_MODE = "live"

# Период полного цикла обновления коэффициентов, сек (Live-режим по ТЗ — 10 с)
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "10"))

# Случайная задержка между запросами к ОДНОЙ БК, сек (защита от блокировок)
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "2"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "5"))

# Порог доходности для звукового уведомления, %
SOUND_ALERT_PROFIT = float(os.getenv("SOUND_ALERT_PROFIT", "2.5"))

# Банки, для которых рассчитываются суммы ставок, ₽
BANKS = [1000, 5000, 10000]

# Файл базы данных SQLite (история найденных вилок)
DB_PATH = os.getenv("DB_PATH", "arbs.sqlite3")

# Таймаут HTTP-запросов к БК, сек
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))

# Fonbet: домен сервера линии периодически меняется. Если авто-перебор
# кандидатов не сработал, впишите сюда актуальный хост из вкладки Network
# браузера (запрос events/list или events/listBase), например:
#   FONBET_LINE_HOST=line52.bkfon-resources.com
FONBET_LINE_HOST = os.getenv("FONBET_LINE_HOST", "").strip()

# Использовать Selenium для БК с динамическими страницами (Winline / BetBoom /
# Лига Ставок). Требует установленных selenium + Chromium (см. README).
USE_SELENIUM = os.getenv("USE_SELENIUM", "1") not in ("0", "false", "no", "")
