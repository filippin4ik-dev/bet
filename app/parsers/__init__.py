"""Парсеры букмекерских контор (любые двухисходные рынки).

Работаем только с реальными данными БК — демо-режима нет.
"""
import logging

from ..config import LIGASTAVOK_ENABLED
from .base import BaseParser
from .bcgame import BCGameParser
from .betboom import BetBoomParser
from .fonbet import FonbetParser
from .ligastavok import LigaStavokParser
from .winline import WinlineParser

log = logging.getLogger("parsers")

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "bc.game"] + \
    (["Liga Stavok"] if LIGASTAVOK_ENABLED else [])

_ls_notice_shown = False


def get_parsers() -> list[BaseParser]:
    global _ls_notice_shown
    parsers: list[BaseParser] = [WinlineParser(), BetBoomParser(),
                                 FonbetParser(), BCGameParser()]
    if LIGASTAVOK_ENABLED:
        parsers.append(LigaStavokParser())
    elif not _ls_notice_shown:
        log.info(
            "Лига Ставок отключена: её Qrator блокирует IP дата-центров, "
            "нужен резидентный прокси. Задайте LIGASTAVOK_PROXY (включится "
            "автоматически) или LIGASTAVOK_ENABLED=1 (см. README).")
        _ls_notice_shown = True
    return parsers
