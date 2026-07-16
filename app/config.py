"""Настройки приложения (переопределяются переменными окружения)."""
import os

# Режим работы сканера:
#   demo — встроенный генератор котировок (сайт работает сразу, без доступа к БК)
#   live — реальный парсинг Winline / BetBoom / Fonbet / Liga Stavok
SCANNER_MODE = os.getenv("SCANNER_MODE", "demo").lower()

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
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "8"))
