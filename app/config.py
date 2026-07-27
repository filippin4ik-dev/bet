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

# Вилки с доходностью выше этого порога почти наверняка НЕ вилки, а ошибки
# сопоставления (разные рынки/матчи у разных БК) — такие отбрасываются.
# Реальные прематч-вилки редко превышают 10-15%.
ARB_MAX_PROFIT = float(os.getenv("ARB_MAX_PROFIT", "25"))

# Допуск расхождения времени начала матча между БК, сек. Если время старта
# у двух БК отличается сильнее — это РАЗНЫЕ матчи одной пары команд
# (первый/ответный, мужской/женский), их кэфы не сшиваются в одну вилку.
START_TS_TOLERANCE = float(os.getenv("START_TS_TOLERANCE", "1800"))

# Тот же допуск для ЕДИНОБОРСТВ (MMA/бокс и т.п.). Время боя на карде —
# оценочное, у разных БК оно расходится на часы (проверено: bc.game 21:45 /
# BetBoom 23:00 для одного боя UFC) — с обычным допуском бой разваливался
# на два «события» и вилки терялись. Одна пара бойцов не дерётся дважды
# за день, поэтому окно можно держать большим (12 часов).
START_TS_TOLERANCE_COMBAT = float(
    os.getenv("START_TS_TOLERANCE_COMBAT", "43200"))

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

# ---- Winline: прямой websocket-фид линии (data_ng) ----
# Winline парсится без браузера: подключаемся к тому же бинарному фиду,
# что и сайт, и держим соединение постоянно (см. app/parsers/wl_feed.py).
WINLINE_FEED_URL = os.getenv(
    "WINLINE_FEED_URL", "wss://wss.winline.ru/data_ng?client=newsite&nb=true")
# Сколько секунд ждать первый снапшот прематча после подключения
# (обычно приходит за 2-5 c, запас на медленную сеть).
WINLINE_SNAPSHOT_WAIT = float(os.getenv("WINLINE_SNAPSHOT_WAIT", "30"))
# Если кадры фида не приходили дольше этого времени — состояние считается
# протухшим и парсер отдаёт 0 котировок (сканер удержит старые до ODDS_TTL).
WINLINE_STALE_AFTER = float(os.getenv("WINLINE_STALE_AFTER", "120"))
# Полная роспись событий Winline («event.plus»): снапшот прематча содержит
# только топ-линии (~10 на матч), полная роспись даёт в разы больше рынков
# (все линии тоталов/фор, таймы, периоды). 0 — отключить.
WINLINE_PLUS_ENABLED = os.getenv("WINLINE_PLUS_ENABLED", "1") not in (
    "0", "false", "no")
# Сколько запросов полной росписи слать в секунду (сайт шлёт такой запрос
# при каждом открытии страницы события — умеренный поток нормален).
WINLINE_PLUS_RATE = float(os.getenv("WINLINE_PLUS_RATE", "15"))
# Раз в сколько секунд обновлять полную роспись каждого события.
# При ~2000 прематч-событий и 15 запр/с полный круг занимает ~2-3 минуты.
WINLINE_PLUS_REFRESH = float(os.getenv("WINLINE_PLUS_REFRESH", "300"))

# Использовать Selenium для БК с динамическими страницами (Лига Ставок).
# Требует установленных selenium + Chromium (см. README).
USE_SELENIUM = os.getenv("USE_SELENIUM", "1") not in ("0", "false", "no", "")

# Лига Ставок закрыта анти-ботом Qrator: браузер должен успеть решить
# JS-челлендж (proof-of-work + перезагрузка) до того, как парсер заберёт
# DOM. Сколько секунд ждать прохождения челленджа и отрисовки линии.
# ВАЖНО: Qrator пропускает только российские «жилые» IP; с IP дата-центров
# челлендж отклоняется (403) — тогда Лига Ставок отдаст 0 котировок.
LIGASTAVOK_CHALLENGE_WAIT = float(os.getenv("LIGASTAVOK_CHALLENGE_WAIT",
                                            "45"))
# Внутренний JSON-API линии Лиги Ставок (быстрый путь: структурированные
# данные вместо парсинга HTML). Требует пройденного Qrator-cookie (их
# добывает браузер). Хост можно переопределить, если сменится.
LIGASTAVOK_API_HOST = os.getenv(
    "LIGASTAVOK_API_HOST", "https://lds-api-sites.ligastavok.ru").rstrip("/")
# Сколько страниц линии максимум забирать (по LIGASTAVOK_API_PAGE событий).
LIGASTAVOK_API_MAX_PAGES = int(os.getenv("LIGASTAVOK_API_MAX_PAGES", "40"))
LIGASTAVOK_API_PAGE = int(os.getenv("LIGASTAVOK_API_PAGE", "100"))
# Резидентный («жилой») российский прокси ТОЛЬКО для Лиги Ставок: Qrator
# жёстко блокирует IP дата-центров (403-заглушка даже настоящему браузеру,
# проверено на московском VPS). Формат: http://user:pass@host:port или
# socks5://user:pass@host:port. Применяется к HTTP-запросам парсера и к
# браузеру (браузеру — только host:port, логин/пароль Chrome не умеет:
# берите прокси с авторизацией по IP, это стандартная опция провайдеров).
LIGASTAVOK_PROXY = os.getenv("LIGASTAVOK_PROXY", "").strip()

# Включена ли Лига Ставок. По умолчанию — АВТО: парсер работает, только
# если задан резидентный прокси (LIGASTAVOK_PROXY). Без прокси с серверного
# IP Qrator всё равно отдаёт заглушку, а каждый цикл впустую тратил ~50 с
# на рендер браузером. Явное значение: LIGASTAVOK_ENABLED=1 — включить
# всегда (например, «жилой» IP без прокси), 0 — выключить всегда.
_LS_RAW = os.getenv("LIGASTAVOK_ENABLED", "").strip().lower()
if _LS_RAW in ("1", "true", "yes"):
    LIGASTAVOK_ENABLED = True
elif _LS_RAW in ("0", "false", "no"):
    LIGASTAVOK_ENABLED = False
else:
    LIGASTAVOK_ENABLED = bool(LIGASTAVOK_PROXY)

# Сколько секунд максимум прокручивать страницу, чтобы SPA дорисовала ВСЕ
# матчи (динамические сайты рендерят список лениво — без прокрутки видна
# только верхушка линии, ~50-60 событий).
SCROLL_SECONDS = float(os.getenv("SCROLL_SECONDS", "25"))

# BetBoom парсится не через браузер, а через прямой websocket-фид линии
# (sporthub). Сколько максимум секунд собирать всю прематч-линию за цикл:
# обычно хватает ~10-15 c на несколько тысяч матчей, ставим запас.
BETBOOM_FEED_TIMEOUT = float(os.getenv("BETBOOM_FEED_TIMEOUT", "90"))
# Лайв-линия меньше прематча — на её сбор нужно меньше времени.
BETBOOM_LIVE_FEED_TIMEOUT = float(os.getenv("BETBOOM_LIVE_FEED_TIMEOUT", "30"))
# Забирать ПОЛНУЮ роспись каждого матча BetBoom (все рынки: таймы, карты,
# угловые, ЖК и т.д.), а не только топ-ставки из дерева турниров.
# Стоит ~10 МБ трафика на цикл прематча; 0 — только топ-ставки.
BETBOOM_FULL_MARKETS = os.getenv("BETBOOM_FULL_MARKETS", "1") not in (
    "0", "false", "no")

# ---- bc.game: публичный REST-фид BetBy (sptpub) ----
# bc.game использует спортивную платформу BetBy. Российское «зеркало» фида
# отдаёт линию без авторизации. Хост/brand периодически меняются: brand_id
# парсер пытается узнать с сайта, а хост можно переопределить здесь.
BCGAME_API_HOST = os.getenv("BCGAME_API_HOST",
                            "https://cocoesports.com").rstrip("/")
# brand_id BetBy по умолчанию (актуализируется автоматически с bc.game).
BCGAME_BRAND_ID = os.getenv("BCGAME_BRAND_ID", "2103509236163162112")
# Эндпоинт bc.game, отдающий актуальный brand_id провайдера BetBy.
BCGAME_PROVIDER_URL = os.getenv(
    "BCGAME_PROVIDER_URL",
    "https://bc.game/api/platform-sports/v14/home/sport/provider/support/")
# Язык линии (ru — русские названия команд/рынков для сопоставления с БК РФ).
BCGAME_LANG = os.getenv("BCGAME_LANG", "ru")

# ---- Лайв-режим (матчи в игре) ----
# Лайв сканируется ОТДЕЛЬНЫМ быстрым циклом: коэффициенты в игре меняются
# ежесекундно, поэтому опрос чаще и котировки живут недолго.
# Акцент сканера — ПРЕМАТЧ: лайв можно полностью выключить (LIVE_ENABLED=0),
# тогда ресурсы CPU/сети целиком уходят прематч-циклу (полезно на слабом VPS).
LIVE_ENABLED = os.getenv("LIVE_ENABLED", "1") not in ("0", "false", "no")
LIVE_SCAN_INTERVAL = float(os.getenv("LIVE_SCAN_INTERVAL", "12"))
# TTL лайв-котировок: если БК не обновляла их дольше — считаем протухшими
# (в лайве старый кэф опаснее отсутствия — на нём нельзя ставить).
LIVE_ODDS_TTL = float(os.getenv("LIVE_ODDS_TTL", "45"))
# Пауза между СОБСТВЕННЫМИ обновлениями одной БК в лайве. Каждая БК в лайве
# обновляется в своём потоке и как можно чаще (быстрая БК не ждёт медленную);
# эта пауза не даёт долбить сервер БК вплотную. 0 — без искусственной паузы.
LIVE_PER_BK_GAP = float(os.getenv("LIVE_PER_BK_GAP", "1.5"))

# ---- Админка (аккаунты БК, баланс-лимиты, авто-ставки) ----
# Логин/пароль по умолчанию admin/admin1 — ОБЯЗАТЕЛЬНО смените в проде
# через переменные окружения ADMIN_USERNAME/ADMIN_PASSWORD.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1")

# Секрет для подписи сессионных cookie и шифрования логинов/паролей БК
# в базе. Если не задан явно — генерируется один раз и сохраняется в файл
# рядом с базой (переживает перезапуск, но НЕ коммитьте этот файл).
SECRET_KEY_FILE = os.getenv("SECRET_KEY_FILE", ".secret_key")
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

# Сколько живёт сессия админки, сек (по умолчанию 12 часов)
ADMIN_SESSION_TTL = float(os.getenv("ADMIN_SESSION_TTL", "43200"))

# Раз в сколько секунд авто-обновлять баланс подключённых аккаунтов БК.
BALANCE_REFRESH_INTERVAL = float(os.getenv("BALANCE_REFRESH_INTERVAL", "300"))

# Авто-ставки на вилки (ОЧЕНЬ рискованная функция: букмекеры банят аккаунты
# за вилочников, а автоматизация браузера может ошибиться в выборе исхода
# или размере ставки). Выключено по умолчанию — включает оператор осознанно.
AUTOBET_ENABLED = os.getenv("AUTOBET_ENABLED", "0") not in ("0", "false", "no", "")
# Пока не выключен явно — авто-ставка выполняется В РЕЖИМЕ ИМИТАЦИИ (dry-run):
# бот проходит весь расчёт и «нажимает» кнопки в логике, но реального запроса
# на сайт БК на размещение ставки не делает. Реальные ставки — только когда
# AUTOBET_DRY_RUN=0 И AUTOBET_ENABLED=1 одновременно.
AUTOBET_DRY_RUN = os.getenv("AUTOBET_DRY_RUN", "1") not in ("0", "false", "no", "")
# Не превышать этот процент от баланса аккаунта одной ставкой (защита от
# ошибок в расчёте лимита при неточном балансе).
AUTOBET_MAX_BALANCE_FRACTION = float(
    os.getenv("AUTOBET_MAX_BALANCE_FRACTION", "0.9"))
# Верхний потолок одной ставки в рублях (доп. защита, даже если баланс больше).
AUTOBET_MAX_STAKE = float(os.getenv("AUTOBET_MAX_STAKE", "5000"))
