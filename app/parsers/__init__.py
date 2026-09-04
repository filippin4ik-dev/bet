"""Парсеры букмекерских контор (любые двухисходные рынки).

Работаем только с реальными данными БК — демо-режима нет.
"""
import logging

from ..config import (BCGAME_ENABLED, BOOKMAKERS_ONLY, LIGASTAVOK_ENABLED,
                      MELBET_ENABLED, ROOBET_ENABLED)
from .base import BaseParser
from .bcgame import BCGameParser
from .betboom import BetBoomParser
from .betcity import BetcityParser
from .fonbet import FonbetParser
from .leon import LeonParser
from .ligastavok import LigaStavokParser
from .melbet import MelbetParser
from .roobet import RoobetParser
from .winline import WinlineParser

log = logging.getLogger("parsers")

def _wanted(name: str) -> bool:
    """Просили ли эту БК в BOOKMAKERS. Пустой список — берём все."""
    return not BOOKMAKERS_ONLY or name.strip().lower() in BOOKMAKERS_ONLY


BOOKMAKERS = [b for b in
              ["Winline", "BetBoom", "Fonbet", "LeonBet", "Betcity"] +
              (["Melbet"] if MELBET_ENABLED else []) +
              (["bc.game"] if BCGAME_ENABLED else []) +
              (["Roobet"] if ROOBET_ENABLED else []) +
              (["Liga Stavok"] if LIGASTAVOK_ENABLED else [])
              if _wanted(b)]

_ls_notice_shown = False
_bc_notice_shown = False
_mb_notice_shown = False
_rb_notice_shown = False


def get_parsers() -> list[BaseParser]:
    global _ls_notice_shown, _bc_notice_shown, _mb_notice_shown
    global _rb_notice_shown
    parsers: list[BaseParser] = [WinlineParser(), BetBoomParser(),
                                 FonbetParser(), LeonParser(),
                                 BetcityParser()]
    if MELBET_ENABLED:
        parsers.append(MelbetParser())
    elif not _mb_notice_shown:
        log.info(
            "Melbet отключена (MELBET_ENABLED=0): её фид отвечает только "
            "российским адресам. MELBET_ENABLED=1 — включить обратно.")
        _mb_notice_shown = True
    if BCGAME_ENABLED:
        parsers.append(BCGameParser())
    elif not _bc_notice_shown:
        log.info(
            "bc.game отключена (BCGAME_ENABLED=0): единственная крипто-БК "
            "в наборе (деньги в USDT, курс задаётся в админке). "
            "BCGAME_ENABLED=1 — включить обратно.")
        _bc_notice_shown = True
    if ROOBET_ENABLED:
        parsers.append(RoobetParser())
    elif not _rb_notice_shown:
        log.info(
            "Roobet отключена (ROOBET_ENABLED=0): крипто-БК на той же "
            "платформе BetBy, что и bc.game (между собой они в вилку не "
            "сшиваются, см. BOOKMAKER_FAMILIES). ROOBET_ENABLED=1 — "
            "включить обратно.")
        _rb_notice_shown = True
    if LIGASTAVOK_ENABLED:
        parsers.append(LigaStavokParser())
    elif not _ls_notice_shown:
        log.info(
            "Лига Ставок отключена: её Qrator блокирует IP дата-центров, "
            "нужен резидентный прокси. Задайте LIGASTAVOK_PROXY (включится "
            "автоматически) или LIGASTAVOK_ENABLED=1 (см. README).")
        _ls_notice_shown = True
    if BOOKMAKERS_ONLY:
        parsers = [p for p in parsers if _wanted(p.name)]
        log.info("Обход ограничен списком BOOKMAKERS: %s",
                 ", ".join(p.name for p in parsers) or "(пусто!)")
    return parsers
