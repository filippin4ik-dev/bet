"""Парсеры букмекерских контор (только рынок П1/П2)."""
from ..config import SCANNER_MODE
from .base import BaseParser
from .betboom import BetBoomParser
from .demo import DemoParser
from .fonbet import FonbetParser
from .ligastavok import LigaStavokParser
from .winline import WinlineParser

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok"]


def get_parsers() -> list[BaseParser]:
    if SCANNER_MODE == "live":
        return [WinlineParser(), BetBoomParser(), FonbetParser(), LigaStavokParser()]
    # demo: те же 4 БК, но котировки из встроенного генератора
    return [DemoParser(name) for name in BOOKMAKERS]
