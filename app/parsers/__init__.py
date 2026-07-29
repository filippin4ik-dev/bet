"""Парсеры букмекерских контор (любые двухисходные рынки).

Работаем только с реальными данными БК — демо-режима нет.
"""
import logging

from ..config import BCGAME_ENABLED, LIGASTAVOK_ENABLED
from .base import BaseParser
from .bcgame import BCGameParser
from .betboom import BetBoomParser
from .betcity import BetcityParser
from .fonbet import FonbetParser
from .leon import LeonParser
from .ligastavok import LigaStavokParser
from .winline import WinlineParser

log = logging.getLogger("parsers")

BOOKMAKERS = ["Winline", "BetBoom", "Fonbet", "LeonBet", "Betcity"] + \
    (["bc.game"] if BCGAME_ENABLED else []) + \
    (["Liga Stavok"] if LIGASTAVOK_ENABLED else [])

_ls_notice_shown = False
_bc_notice_shown = False


def get_parsers() -> list[BaseParser]:
    global _ls_notice_shown, _bc_notice_shown
    parsers: list[BaseParser] = [WinlineParser(), BetBoomParser(),
                                 FonbetParser(), LeonParser(),
                                 BetcityParser()]
    if BCGAME_ENABLED:
        parsers.append(BCGameParser())
    elif not _bc_notice_shown:
        log.info(
            "bc.game отключена (BCGAME_ENABLED=0): единственная зарубежная "
            "БК в наборе, её линия и написание имён команд заметно "
            "расходятся с БК РФ. BCGAME_ENABLED=1 — включить обратно.")
        _bc_notice_shown = True
    if LIGASTAVOK_ENABLED:
        parsers.append(LigaStavokParser())
    elif not _ls_notice_shown:
        log.info(
            "Лига Ставок отключена: её Qrator блокирует IP дата-центров, "
            "нужен резидентный прокси. Задайте LIGASTAVOK_PROXY (включится "
            "автоматически) или LIGASTAVOK_ENABLED=1 (см. README).")
        _ls_notice_shown = True
    return parsers
