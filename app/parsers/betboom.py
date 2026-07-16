"""BetBoom — динамический сайт (React SPA), парсится через Selenium.

Вёрстка использует обфусцированные классы (bb-xxx), поэтому вместо CSS-классов
разбираем последовательность текстовых токенов внутри карточки события.

Структура карточки (проверена на живой странице):
    [Команда1, (счёт...), Команда2, (счёт...), статус, 'П1', k1, 'X', kX,
     'П2', k2, 'Ещё', '+ N']
Берём только П1/П2 (исход матча). Ничью (X) игнорируем — рынок остаётся
двухисходным. Тоталы на карточке в списке не отображаются (нужен заход в
событие), поэтому здесь только победитель.

При смене вёрстки правьте разбор ниже; логика поиска вилок не меняется.
"""
import re

from bs4 import BeautifulSoup

from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import num
from .selenium_helper import SeleniumSession

# Двухисходные виды спорта и их slug'и на betboom.ru (без футбола/гандбола —
# там есть ничья). Обходим каждый вид отдельно: на общей странице доминирует
# футбол, а нам нужны только рынки без ничьей.
SPORTS = [
    ("tennis", "Теннис"),
    ("table-tennis", "Настольный теннис"),
    ("basketball", "Баскетбол"),
    ("volleyball", "Волейбол"),
    ("ice-hockey", "Хоккей"),
    ("esports", "Киберспорт"),
]
# (шаблон URL, тип рынка)
SECTIONS = [
    ("https://betboom.ru/sport/live/{slug}", KIND_LIVE),
    ("https://betboom.ru/sport/line/{slug}", KIND_PREMATCH),
]

_DEC = re.compile(r"^\d+\.\d{1,3}$")

# Служебные подписи статуса матча — не команды
_STATUS_WORDS = (
    "прерван", "не начал", "перерыв", "заверш", "отмен", "перенес",
    "пауза", "ещё", "eще", "матч",
)


# Прематч меняется медленно, а обход всех видов спорта долгий — обновляем
# прематч-страницы только каждый N-й цикл, live — каждый цикл.
PREMATCH_EVERY = 5


class BetBoomParser(BaseParser):
    name = "BetBoom"

    def __init__(self) -> None:
        super().__init__()
        self._cycle = 0
        self._prematch_cache: list[MarketOdds] = []

    def fetch_odds(self) -> list[MarketOdds]:
        self._cycle += 1
        scan_prematch = (self._cycle - 1) % PREMATCH_EVERY == 0
        odds: list[MarketOdds] = []
        with SeleniumSession() as s:
            if s.driver is None:
                return []
            for tmpl, kind in SECTIONS:
                if kind == KIND_PREMATCH and not scan_prematch:
                    continue
                page_odds: list[MarketOdds] = []
                for slug, sport in SPORTS:
                    self._delay()
                    html = s.render(tmpl.format(slug=slug), wait_seconds=15)
                    if html:
                        page_odds.extend(self._parse_html(html, kind, sport))
                if kind == KIND_PREMATCH:
                    self._prematch_cache = page_odds
                odds.extend(page_odds)
        if not scan_prematch:
            odds.extend(self._prematch_cache)
        return odds

    def _parse_html(self, html: str, kind: str,
                    sport: str = "Спорт") -> list[MarketOdds]:
        soup = BeautifulSoup(html, "html.parser")
        result: list[MarketOdds] = []

        # Карточки событий: узлы, содержащие маркер 'П1' и хотя бы 2 кэфа.
        for card in soup.select("div"):
            # только «листовые» карточки, без вложенных карточек
            if card.find("div", recursive=False) and card.select("div div div div div"):
                pass
            toks = [t.strip() for t in card.stripped_strings]
            if "П1" not in toks or "П2" not in toks:
                continue
            # чтобы не брать контейнер целой лиги — в карточке одна связка П1..П2
            if toks.count("П1") != 1 or toks.count("П2") != 1:
                continue

            parsed = self._parse_card(toks, kind, sport)
            if parsed:
                result.append(parsed)
        # Удаляем дубли (одно и то же событие ловится на нескольких уровнях DOM)
        return _dedup(result)

    def _parse_card(self, toks: list[str], kind: str,
                    sport: str) -> MarketOdds | None:
        i1 = toks.index("П1")
        # k1 идёт сразу после 'П1'
        k1 = num(toks[i1 + 1]) if i1 + 1 < len(toks) else None
        # k2 идёт сразу после 'П2'
        i2 = toks.index("П2")
        k2 = num(toks[i2 + 1]) if i2 + 1 < len(toks) else None
        if not k1 or not k2:
            return None

        # Если между П1 и П2 есть ничья 'X' с реальным кэфом — это рынок
        # 1X2 (три исхода, напр. футбол), он НЕ двухисходный. Пропускаем.
        if "X" in toks[i1:i2]:
            xi = toks.index("X", i1, i2)
            if xi + 1 < len(toks) and num(toks[xi + 1]):
                return None

        # Команды — токены до блока со счётом/статусом, до первого 'П1'.
        # Берём непустые нечисловые токены слева, отбрасывая счёт (одиночные
        # цифры) и статус (содержит 'мин', ':' или 'Т,').
        names = []
        for t in toks[:i1]:
            if _DEC.match(t):
                continue
            if re.fullmatch(r"\d+", t):
                continue  # счёт
            if any(m in t for m in ("мин", ":", "Т,", "тайм", "сет", "гейм")):
                continue
            if t in ("Ещё", "X") or len(t) < 2:
                continue  # служебные метки и одиночные символы — не имена
            if any(w in t.lower() for w in _STATUS_WORDS):
                continue  # статус матча, не команда
            names.append(t)
        if len(names) < 2:
            return None
        team1, team2 = names[0], names[-1]
        if team1 == team2:
            return None

        return MarketOdds(
            bookmaker=self.name, sport=sport,
            team1=team1, team2=team2,
            market="Победитель", market_key="winner",
            outcome1="П1", outcome2="П2",
            k1=k1, k2=k2, kind=kind,
        )


def _dedup(items: list[MarketOdds]) -> list[MarketOdds]:
    seen, out = set(), []
    for o in items:
        key = (o.team1, o.team2, o.k1, o.k2)
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out
