"""BetBoom — динамический сайт (React SPA), парсится через Selenium.

Только ПРЕМАТЧ (раздел «Линия»). Особенность сайта: список событий не
рисуется, пока не кликнешь по виду спорта в левом меню — данные приходят
по websocket после клика. Поэтому каждая страница открывается с кликом по
названию вида спорта, затем лента прокручивается до конца (иначе в DOM
только верхние ~30 событий).

Вёрстка использует обфусцированные классы (bb-xxx), поэтому вместо
CSS-классов разбираем последовательность текстовых токенов внутри карточки:
    [Команда1, (рейтинг ATP: 71...), Команда2, (рейтинг...),
     'Завтра в 09:00', 'П1', k1, 'П2', k2, 'Ещё', '+ N']
Берём только П1/П2 (исход матча); рынки с ничьей (X с кэфом) пропускаем.

При смене вёрстки правьте разбор ниже; логика поиска вилок не меняется.
"""
import re
import time

from bs4 import BeautifulSoup

from ..config import BETBOOM_MAX_LEAGUES, SCROLL_SECONDS
from ..models import KIND_PREMATCH, MarketOdds
from .base import BaseParser
from .html_utils import SOUP_PARSER, format_start, num, parse_start_ts
from .selenium_helper import SeleniumSession

# Двухисходные виды спорта: (slug URL, название в левом меню, вид спорта).
# Хоккей не обходим: на карточках BetBoom он всегда 1X2 (с ничьей),
# двухисходных рынков в списке нет — только трата времени цикла.
SPORTS = [
    ("tennis", "Теннис", "Теннис"),
    ("table-tennis", "Настольный теннис", "Настольный теннис"),
    ("basketball", "Баскетбол", "Баскетбол"),
    ("volleyball", "Волейбол", "Волейбол"),
    ("esports", "Кибер", "Киберспорт"),
]
URL_TMPL = "https://betboom.ru/sport/line/{slug}"

_DEC = re.compile(r"^\d+\.\d{1,3}$")

# Служебные подписи статуса матча — не команды
_STATUS_WORDS = (
    "прерван", "не начал", "перерыв", "заверш", "отмен", "перенес",
    "пауза", "ещё", "eще", "матч", "сегодня", "завтра",
)


class BetBoomParser(BaseParser):
    name = "BetBoom"

    def fetch_odds(self) -> list[MarketOdds]:
        by_key: dict[str, MarketOdds] = {}
        with SeleniumSession() as s:
            if s.driver is None:
                return []
            for slug, menu_text, sport in SPORTS:
                self._delay()
                snaps = s.render_league_pages(
                    URL_TMPL.format(slug=slug), click_text=menu_text,
                    wait_seconds=12, scroll_seconds=SCROLL_SECONDS,
                    max_leagues=BETBOOM_MAX_LEAGUES)
                for html in snaps:
                    for o in self._parse_html(html, sport):
                        by_key[o.match_key] = o  # дубли между снимками
        return list(by_key.values())

    def _parse_html(self, html: str, sport: str) -> list[MarketOdds]:
        soup = BeautifulSoup(html, SOUP_PARSER)
        result: list[MarketOdds] = []
        now = time.time()

        # Карточки событий: узлы, содержащие ровно одну связку П1..П2
        for card in soup.select("div"):
            toks = [t.strip() for t in card.stripped_strings]
            if toks.count("П1") != 1 or toks.count("П2") != 1:
                continue

            parsed = self._parse_card(toks, sport, now)
            if parsed:
                result.append(parsed)
        # Одно и то же событие ловится на нескольких уровнях DOM — дубли
        # уберёт fetch_odds по match_key.
        return result

    def _parse_card(self, toks: list[str], sport: str,
                    now: float) -> MarketOdds | None:
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

        # Время начала: распознаём по всему тексту карточки до 'П1'
        # («Завтра в 09:00», «19.07 в 13:10»; токены могут быть разбиты
        # на несколько DOM-узлов, поэтому парсим склеенную строку).
        # Прематч-карточка ВСЕГДА показывает время — карточки без него
        # (live, служебные блоки) пропускаем.
        start_ts = parse_start_ts(" ".join(toks[:i1]), now)
        if start_ts is None or start_ts <= now:
            return None
        start_time = format_start(start_ts)

        # Команды — непустые нечисловые токены до 'П1', без рейтингов
        # (содержат ':'), счёта (одиночные числа) и служебных слов.
        names = []
        for t in toks[:i1]:
            if _DEC.match(t):
                continue
            if re.fullmatch(r"\d+", t):
                continue  # счёт / номер
            if any(m in t for m in ("мин", ":", "Т,", "тайм", "сет", "гейм")):
                continue
            if t in ("Ещё", "X") or len(t) < 2:
                continue  # служебные метки и одиночные символы — не имена
            if any(w in t.lower() for w in _STATUS_WORDS):
                continue  # статус матча, не команда
            names.append(t)
        # В карточке матча остаётся ровно пара команд. Если «имён» много —
        # это контейнер целой страницы (в токены попало меню сайта), а не
        # карточка. Пропускаем, иначе получатся команды вида «Линия».
        if len(names) != 2:
            return None
        team1, team2 = names[0], names[1]
        if team1 == team2:
            return None

        return MarketOdds(
            bookmaker=self.name, sport=sport,
            team1=team1, team2=team2,
            market="Победитель", market_key="winner",
            outcome1="П1", outcome2="П2",
            k1=k1, k2=k2, kind=KIND_PREMATCH,
            start_time=start_time, start_ts=start_ts,
        )
