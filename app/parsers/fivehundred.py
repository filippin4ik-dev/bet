"""500.casino (500.casino/ru/sports) — крипто-казино со спортом на BetBy.

Разбор фида общий с bc.game/Roobet/Rainbet и лежит в betby.py; здесь —
приметы площадки.

brand_id BetBy площадка отдаёт сама: JSON-ручка /api/boot возвращает
настройки сайта, среди них siteSettings.betbyBrandId (и адрес виджета
siteSettings.betbyScriptUrl → csgo500.sptpub.com). Ручка отвечает JSON
только браузеру, прошедшему Cloudflare-проверку (иначе — HTML главной
страницы), так что при неудаче берём значение из config. Собственный узел
виджета csgo500.sptpub.com на адреса дата-центров отвечает «access
blocked», общий узел платформы отдаёт бренд без вопросов.

ВАЖНО: с bc.game, Roobet и Rainbet эта площадка в вилку не сшивается —
линию им считает одна платформа, см. BOOKMAKER_FAMILIES в config.py.
С 1win — сшивается.
"""
import logging

from ..config import (FIVEHUNDRED_API_HOST, FIVEHUNDRED_BOOT_URL,
                      FIVEHUNDRED_BRAND_ID, FIVEHUNDRED_LANG,
                      FIVEHUNDRED_SITE_HOST, FIVEHUNDRED_SPORTS_URL,
                      HTTP_TIMEOUT)
from .betby import BetByParser

log = logging.getLogger("parsers.fivehundred")


class FiveHundredParser(BetByParser):
    name = "500.casino"
    api_host = FIVEHUNDRED_API_HOST
    default_brand = FIVEHUNDRED_BRAND_ID
    lang = FIVEHUNDRED_LANG
    site_url = FIVEHUNDRED_SITE_HOST
    sports_url = FIVEHUNDRED_SPORTS_URL

    def _discover_brand(self) -> str | None:
        if not FIVEHUNDRED_BOOT_URL:
            return None
        headers = self._headers()
        headers["Accept"] = "application/json, text/plain, */*"
        resp = self.session.get(FIVEHUNDRED_BOOT_URL, headers=headers,
                                timeout=HTTP_TIMEOUT)
        if "json" not in (resp.headers.get("Content-Type") or ""):
            return None   # Cloudflare отдал HTML вместо настроек
        brand = (resp.json().get("siteSettings") or {}).get("betbyBrandId")
        return str(brand) if brand else None
