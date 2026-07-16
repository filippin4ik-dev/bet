"""Парсеры букмекерских контор (любые двухисходные рынки).

Работаем только с реальными данными БК — демо-режима нет.
"""
from .base import BaseParser
from .betboom import BetBoomParser
from .fonbet import FonbetParser
from .ligastavok import LigaStavokParser
from .winline import WinlineParser

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "Liga Stavok"]


def get_parsers() -> list[BaseParser]:
    return [WinlineParser(), BetBoomParser(), FonbetParser(), LigaStavokParser()]
