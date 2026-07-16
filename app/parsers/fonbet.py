"""Fonbet — парсинг публичного JSON-API линии (Live + прематч).

Проверяются ВСЕ события линии (включая дочерние росписи — сеты, карты,
периоды) и все подходящие двухисходные рынки:

- Победитель (П1 id=921 / П2 id=923), если НЕТ ничьей (id=922);
- Тоталы больше/меньше (ТБ id=930 / ТМ id=931) с параметром линии `pt`
  (например «убийств больше 2.5 / меньше 2.5», карт, геймов, очков и т.п.).

Каждый тотал — отдельный двухисходный рынок; сравнивается только с точно
таким же тоталом (та же линия pt) у других БК.
"""
from datetime import datetime

from ..models import KIND_LIVE, KIND_PREMATCH, MarketOdds
from .base import BaseParser

# Зеркала JSON-API линии Fonbet (структура одинаковая)
API_URLS = [
    "https://line32.bk6bba-resources.com/events/list?lang=ru&scopeMarket=1600",
    "https://line01.bk6bba-resources.com/events/list?lang=ru&scopeMarket=1600",
]

F_P1, F_DRAW, F_P2 = 921, 922, 923
F_TOTAL_OVER, F_TOTAL_UNDER = 930, 931


class FonbetParser(BaseParser):
    name = "Fonbet"

    def fetch_odds(self) -> list[MarketOdds]:
        data = None
        for url in API_URLS:
            try:
                data = self.get_json(url, delay=True)
                break
            except Exception:  # noqa: BLE001 — пробуем следующее зеркало
                continue
        if not data:
            return []

        sports = {s["id"]: s.get("name", "") for s in data.get("sports", [])}
        events = {e["id"]: e for e in data.get("events", [])}

        result: list[MarketOdds] = []
        for ef in data.get("customFactors", []):
            event = events.get(ef.get("e"))
            if not event:
                continue

            # У дочерних росписей команды берём у родителя, а название
            # росписи добавляем к виду спорта.
            root = event
            hops = 0
            while (not root.get("team1") or not root.get("team2")) \
                    and root.get("parentId") in events and hops < 5:
                root = events[root["parentId"]]
                hops += 1
            team1, team2 = root.get("team1"), root.get("team2")
            if not team1 or not team2:
                continue

            sport = sports.get(root.get("sportId"), "Спорт")
            market_name = event.get("name") if event is not root else None
            if market_name:
                sport = f"{sport} · {market_name}"

            kind = KIND_LIVE if event.get("place") == "live" else KIND_PREMATCH
            start_time = None
            if kind == KIND_PREMATCH and root.get("startTime"):
                start_time = datetime.fromtimestamp(
                    root["startTime"]).strftime("%d.%m %H:%M")

            base = dict(bookmaker=self.name, sport=sport,
                        team1=team1, team2=team2,
                        kind=kind, start_time=start_time)

            factors = ef.get("factors", [])
            result.extend(self._winner(factors, base))
            result.extend(self._totals(factors, base))

        return result

    def _winner(self, factors: list, base: dict) -> list[MarketOdds]:
        vals = {f["f"]: f.get("v") for f in factors}
        if F_DRAW in vals:
            return []  # трёхисходный рынок — пропускаем
        k1, k2 = vals.get(F_P1), vals.get(F_P2)
        if not k1 or not k2:
            return []
        return [MarketOdds(
            market="Победитель", market_key="winner",
            outcome1="П1", outcome2="П2",
            k1=float(k1), k2=float(k2), **base,
        )]

    def _totals(self, factors: list, base: dict) -> list[MarketOdds]:
        # Тоталы приходят парами ТБ/ТМ с одинаковым параметром линии pt
        overs, unders = {}, {}
        for f in factors:
            pt = f.get("pt") or f.get("p")
            if pt is None:
                continue
            if f["f"] == F_TOTAL_OVER:
                overs[str(pt)] = f.get("v")
            elif f["f"] == F_TOTAL_UNDER:
                unders[str(pt)] = f.get("v")
        out = []
        for pt, over in overs.items():
            under = unders.get(pt)
            if not over or not under:
                continue
            out.append(MarketOdds(
                market=f"Тотал {pt}", market_key=f"total:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=float(over), k2=float(under), **base,
            ))
        return out
