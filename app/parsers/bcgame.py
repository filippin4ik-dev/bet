"""bc.game — крипто-казино со спортивным разделом на платформе BetBy.

Весь разбор фида общий и лежит в betby.py; здесь только приметы площадки:
узел фида, бренд и то, как узнать актуальный brand_id с самого сайта.
"""
import logging

from ..config import (BCGAME_API_HOST, BCGAME_BRAND_ID, BCGAME_LANG,
                      BCGAME_PROVIDER_URL, BCGAME_SITE_HOST, HTTP_TIMEOUT)
from .betby import BetByParser

log = logging.getLogger("parsers.bcgame")


class BCGameParser(BetByParser):
    name = "bc.game"
    api_host = BCGAME_API_HOST
    default_brand = BCGAME_BRAND_ID
    lang = BCGAME_LANG
    site_url = BCGAME_SITE_HOST
    sports_url = BCGAME_SITE_HOST + "/sports"

    def _discover_brand(self) -> str | None:
        """bc.game отдаёт brand_id BetBy в списке спортивных провайдеров."""
        data = self.session.post(
            BCGAME_PROVIDER_URL, headers=self._headers(),
            json={}, timeout=HTTP_TIMEOUT).json()
        for prov in data.get("data", {}).get("sportProviders", []):
            brand = (prov.get("betByBrand") or {}).get("brandFlag")
            if prov.get("name") == "betby" and brand:
                return str(brand)
        return None
