"""Модели данных.

Сканер работает ТОЛЬКО с прематчем (матчи, которые ещё не начались).
Работаем с ЛЮБЫМ двухисходным рынком, а не только с победителем матча:
- Победитель (П1 / П2, без ничьей);
- Тотал больше/меньше на пол-линии (напр. «больше 2.5» / «меньше 2.5» —
  сколько будет убийств, карт, геймов, очков и т.п.);
- любой другой рынок ровно с двумя взаимоисключающими исходами.

Вилка всегда считается по одной формуле: 1/К1_max + 1/К2_max < 1,
где К1 — макс. кэф на исход 1, К2 — макс. кэф на исход 2 (у разных БК).
"""
from dataclasses import dataclass, field

# Тип рынка по времени. Live больше не сканируем, константы оставлены
# для совместимости (история в SQLite, старые записи).
KIND_LIVE = "live"
KIND_PREMATCH = "prematch"

# Матч считается начавшимся спустя минуту после времени старта —
# небольшой запас на рассинхрон часов БК и сервера.
_STARTED_GRACE = 60


@dataclass
class MarketOdds:
    """Коэффициенты одной БК на один рынок одного события.

    Обычно рынок двухисходный (k1/k2). Для рынка «Исход 1X2» (победитель
    с ничьей — market_key начинается с «winner1x2») заполняется ТРЕТИЙ
    исход k3/outcome3 (ничья) — тогда рынок трёхисходный и обрабатывается
    отдельным движком поиска вилок (см. arbitrage.find_arbs_1x2)."""

    bookmaker: str          # Winline / BetBoom / Fonbet / Liga Stavok
    sport: str              # вид спорта (+ дочерняя роспись, если есть)
    team1: str
    team2: str
    market: str             # человекочитаемое имя рынка: «Победитель», «Тотал 2.5»
    market_key: str         # ключ рынка для сопоставления между БК
    outcome1: str           # метка исхода 1: «П1», «ТБ 2.5»
    outcome2: str           # метка исхода 2: «П2», «ТМ 2.5»
    k1: float               # коэффициент на исход 1
    k2: float               # коэффициент на исход 2
    kind: str = KIND_PREMATCH      # всегда prematch (live отключён)
    start_time: str | None = None  # время начала (как показывает БК)
    start_ts: float | None = None  # время начала, unix-время (если распознано)
    url: str | None = None         # страница события на сайте БК (deep-link)
    outcome3: str | None = None    # метка 3-го исхода («X» — ничья)
    k3: float | None = None        # коэффициент на 3-й исход (ничья)

    def started(self, now: float) -> bool:
        """Матч уже начался (по распознанному времени старта)?"""
        return self.start_ts is not None and now >= self.start_ts + _STARTED_GRACE

    @property
    def event_key(self) -> str:
        """Ключ события (без рынка) — для подсчёта числа событий."""
        return f"{self.kind}|{self.sport}|{_norm(self.team1)}|{_norm(self.team2)}"

    @property
    def match_key(self) -> str:
        """Ключ конкретного рынка конкретного события: по нему кэфы разных
        БК сопоставляются между собой (одинаковый матч И одинаковый рынок)."""
        return f"{self.event_key}|{self.market_key}"

    def to_dict(self) -> dict:
        return {
            "bookmaker": self.bookmaker,
            "sport": self.sport,
            "match": f"{self.team1} — {self.team2}",
            "market": self.market,
            "outcome1": self.outcome1,
            "outcome2": self.outcome2,
            "outcome3": self.outcome3,
            "k1": self.k1,
            "k2": self.k2,
            "k3": self.k3,
            "kind": self.kind,
            "start_time": self.start_time,
            "start_ts": self.start_ts,
            "url": self.url,
        }


def _norm(name: str) -> str:
    return " ".join(name.lower().replace("ё", "е").split())


@dataclass
class Arb:
    """Найденная двухисходная вилка."""

    match_key: str
    sport: str
    team1: str
    team2: str
    market: str
    outcome1: str
    outcome2: str
    k1_max: float
    k1_bookmaker: str
    k2_max: float
    k2_bookmaker: str
    margin: float               # 1/К1 + 1/К2 (< 1 — вилка)
    profit_pct: float           # доходность, %
    kind: str = KIND_PREMATCH
    start_time: str | None = None
    start_ts: float | None = None
    stakes: dict = field(default_factory=dict)  # {банк: {...}}
    k1_url: str | None = None   # страница события у БК исхода 1
    k2_url: str | None = None   # страница события у БК исхода 2
    # Когда вилка ВПЕРВЫЕ появилась (unix-время). Сканер сохраняет момент
    # первого обнаружения по match_key, пока вилка жива между обновлениями;
    # пропала и появилась снова — таймер начинается заново.
    first_seen: float | None = None
    # Почему вилка НЕ показана в основном списке (см.
    # arbitrage._reject_reason):
    # код причины и её человекочитаемое объяснение. У показанных вилок оба
    # поля пустые — заполняются только у отсеянных кандидатов, которые
    # уходят на вкладку «Отсеянные» для ручного разбора.
    reject_code: str | None = None
    reject_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "match_key": self.match_key,
            "kind": self.kind,
            "start_time": self.start_time,
            "start_ts": self.start_ts,
            "sport": self.sport,
            "match": f"{self.team1} — {self.team2}",
            "team1": self.team1,
            "team2": self.team2,
            "market": self.market,
            "outcome1": self.outcome1,
            "outcome2": self.outcome2,
            "k1_max": self.k1_max,
            "k1_bookmaker": self.k1_bookmaker,
            "k1_url": self.k1_url,
            "k2_max": self.k2_max,
            "k2_bookmaker": self.k2_bookmaker,
            "k2_url": self.k2_url,
            "margin": round(self.margin, 4),
            "profit_pct": round(self.profit_pct, 2),
            "stakes": self.stakes,
            "first_seen": self.first_seen,
            "reject_code": self.reject_code,
            "reject_reason": self.reject_reason,
        }


@dataclass
class Arb3:
    """Найденная ТРЁХисходная вилка (рынок «Исход 1X2»: П1 / X / П2).

    Формула: 1/К1_max + 1/Кx_max + 1/К2_max < 1, где три коэффициента
    могут быть у трёх РАЗНЫХ БК (или у двух, если третье плечо совпадает
    с одним из них — это не мешает вилке, лишь две ставки идут в одну БК).
    """

    match_key: str
    sport: str
    team1: str
    team2: str
    market: str
    outcome1: str
    outcomex: str
    outcome2: str
    k1_max: float
    k1_bookmaker: str
    kx_max: float
    kx_bookmaker: str
    k2_max: float
    k2_bookmaker: str
    margin: float
    profit_pct: float
    kind: str = KIND_PREMATCH
    start_time: str | None = None
    start_ts: float | None = None
    stakes: dict = field(default_factory=dict)
    k1_url: str | None = None
    kx_url: str | None = None
    k2_url: str | None = None
    first_seen: float | None = None
    reject_code: str | None = None
    reject_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "match_key": self.match_key,
            "kind": self.kind,
            "start_time": self.start_time,
            "start_ts": self.start_ts,
            "sport": self.sport,
            "match": f"{self.team1} — {self.team2}",
            "team1": self.team1,
            "team2": self.team2,
            "market": self.market,
            "outcome1": self.outcome1,
            "outcomex": self.outcomex,
            "outcome2": self.outcome2,
            "k1_max": self.k1_max,
            "k1_bookmaker": self.k1_bookmaker,
            "k1_url": self.k1_url,
            "kx_max": self.kx_max,
            "kx_bookmaker": self.kx_bookmaker,
            "kx_url": self.kx_url,
            "k2_max": self.k2_max,
            "k2_bookmaker": self.k2_bookmaker,
            "k2_url": self.k2_url,
            "margin": round(self.margin, 4),
            "profit_pct": round(self.profit_pct, 2),
            "stakes": self.stakes,
            "first_seen": self.first_seen,
            "reject_code": self.reject_code,
            "reject_reason": self.reject_reason,
        }
