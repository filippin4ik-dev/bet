"""Парсеры букмекерских контор (любые двухисходные рынки).

Работаем только с реальными данными БК — демо-режима нет.
"""
from .base import BaseParser
from .bcgame import BCGameParser
from .betboom import BetBoomParser
from .fonbet import FonbetParser
from .ligastavok import LigaStavokParser
from .winline import WinlineParser

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok", "bc.game"]


def get_parsers() -> list[BaseParser]:
    return [WinlineParser(), BetBoomParser(), FonbetParser(),
            LigaStavokParser(), BCGameParser()]
