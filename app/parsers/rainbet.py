"""Rainbet (rainbet.com/ru/sportsbook) — крипто-казино со спортом на BetBy.

Разбор фида общий с bc.game/Roobet/500.casino и лежит в betby.py; здесь —
приметы площадки.

Сам сайт и его API (services.rainbet.com) закрыты Cloudflare для адресов
дата-центров: каждая ручка отвечает страницей «Checking your browser…», а
страница спортивного раздела — Turnstile-проверкой. Поэтому brand_id с
сайта не спрашиваем: он снят с бандла страницы /sportsbook (вызов
`BTRenderer.initialize({brand_id: …})`) и лежит в config; сменится —
новый виден там же во вкладке Network браузера. Собственный узел виджета
(rainbet.sportsbookcdn.com) линию не отдаёт (404), общий узел платформы
отдаёт бренд Rainbet спокойно — с него и берём.

ВАЖНО: с bc.game, Roobet и 500.casino эта площадка в вилку не сшивается —
линию им считает одна платформа, см. BOOKMAKER_FAMILIES в config.py.
С 1win — сшивается.
"""
from ..config import (RAINBET_API_HOST, RAINBET_BRAND_ID, RAINBET_LANG,
                      RAINBET_SITE_HOST, RAINBET_SPORTS_URL)
from .betby import BetByParser


class RainbetParser(BetByParser):
    name = "Rainbet"
    api_host = RAINBET_API_HOST
    default_brand = RAINBET_BRAND_ID
    lang = RAINBET_LANG
    site_url = RAINBET_SITE_HOST
    sports_url = RAINBET_SPORTS_URL
