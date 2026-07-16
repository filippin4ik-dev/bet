"""Настройки приложения (переопределяются переменными окружения)."""
import os

# Сканер работает только с реальными данными БК (Winline / BetBoom / Fonbet /
# Liga Stavok) и только с ПРЕМАТЧЕМ — live-ставки отключены.
SCANNER_MODE = "prematch"

# Период полного цикла обновления коэффициентов, сек.
# Прематч меняется медленно, а полный обход всех БК занимает минуты,
# поэтому частый опрос не нужен.
SCAN_INTERVAL = float(os.getenv("SCAN_INTERVAL", "30"))

# Случайная задержка между запросами к ОДНОЙ БК, сек (защита от блокировок)
REQUEST_DELAY_MIN = float(os.getenv("REQUEST_DELAY_MIN", "2"))
REQUEST_DELAY_MAX = float(os.getenv("REQUEST_DELAY_MAX", "5"))

# Сколько секунд держать котировки БК, если очередной обход не дал данных
# (сбой сети/рендеринга). Старые матчи не «слетают», пока не истёк TTL;
# начавшиеся матчи удаляются сразу по времени старта.
ODDS_TTL = float(os.getenv("ODDS_TTL", "1800"))

# Часовой пояс, в котором БК показывают время начала матчей (МСК = UTC+3)
BK_TZ_OFFSET = float(os.getenv("BK_TZ_OFFSET", "3"))

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
#   FONBET_LINE_HOST=line01w.bk6bba-resources.com
FONBET_LINE_HOST = os.getenv("FONBET_LINE_HOST", "").strip()

# Использовать Selenium для БК с динамическими страницами (Winline / BetBoom /
# Лига Ставок). Требует установленных selenium + Chromium (см. README).
USE_SELENIUM = os.getenv("USE_SELENIUM", "1") not in ("0", "false", "no", "")

# Сколько секунд максимум прокручивать страницу, чтобы SPA дорисовала ВСЕ
# матчи (Winline/BetBoom рендерят список лениво — без прокрутки видна только
# верхушка линии, ~50-60 событий).
SCROLL_SECONDS = float(os.getenv("SCROLL_SECONDS", "25"))

# BetBoom показывает развёрнутой только первую лигу вида спорта, остальные
# лиги — отдельные страницы. Сколько лиг обходить на каждый вид спорта
# (больше лиг = больше матчей, но заметно дольше цикл).
BETBOOM_MAX_LEAGUES = int(os.getenv("BETBOOM_MAX_LEAGUES", "8"))
