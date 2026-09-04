"""Roobet (зеркало roo916.com) — крипто-казино со спортом на платформе BetBy.

Разбор фида общий с bc.game и лежит в betby.py; здесь — приметы площадки.

Две особенности против bc.game.

1. Собственный узел фида Roobet (roobet.sptpub.com) на запрос с адреса
   дата-центра отвечает `{"error": "access blocked"}`, а общий узел
   платформы (тот же, с которого берёт линию bc.game) тот же самый бренд
   отдаёт спокойно. Поэтому по умолчанию ходим на общий узел, а свой
   оставлен настройкой ROOBET_API_HOST на случай, если у сервера «жилой»
   IP.

2. Сам сайт roobet.com и его зеркало roo916.com закрыты Cloudflare, и
   brand_id со страницы не забрать. Зато GraphQL-ручка каталога отвечает
   без авторизации и отдаёт ровно его (getSportsbookConfig.brandId) —
   с неё и спрашиваем, а при неудаче берём значение из config.

Ссылки на событие ведут на ROOBET_SPORTS_URL — по умолчанию зеркало
roo916.com, потому что основной домен во многих странах недоступен.

ВАЖНО: с bc.game эта площадка в вилку не сшивается — линию им считает одна
платформа, см. BOOKMAKER_FAMILIES в config.py.
"""
import logging

from ..config import (HTTP_TIMEOUT, ROOBET_API_HOST, ROOBET_BRAND_ID,
                      ROOBET_GRAPHQL_URL, ROOBET_LANG, ROOBET_SITE_HOST,
                      ROOBET_SPORTS_URL)
from .betby import BetByParser

log = logging.getLogger("parsers.roobet")

_BRAND_QUERY = {
    "operationName": "GetSportsbookConfig",
    "query": "query GetSportsbookConfig "
             "{ getSportsbookConfig { brandId } }",
    "variables": {},
}


class RoobetParser(BetByParser):
    name = "Roobet"
    api_host = ROOBET_API_HOST
    default_brand = ROOBET_BRAND_ID
    lang = ROOBET_LANG
    site_url = ROOBET_SITE_HOST
    sports_url = ROOBET_SPORTS_URL

    def _discover_brand(self) -> str | None:
        if not ROOBET_GRAPHQL_URL:
            return None
        data = self.session.post(
            ROOBET_GRAPHQL_URL, headers=self._headers(),
            json=_BRAND_QUERY, timeout=HTTP_TIMEOUT).json()
        brand = ((data.get("data") or {}).get("getSportsbookConfig")
                 or {}).get("brandId")
        return str(brand) if brand else None
