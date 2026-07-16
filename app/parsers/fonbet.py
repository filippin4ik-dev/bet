"""Fonbet — парсинг публичного JSON-API линии (Live + прематч).

Проверяются ВСЕ события линии, включая дочерние росписи (сеты, периоды,
карты и т.п.). Подходит любая котировка с двумя исходами: есть факторы
П1 (id=921) и П2 (id=923), но НЕТ ничьей (id=922).
Live/прематч различаем по полю события place: "live" | "line".
"""
from datetime import datetime

from ..models import KIND_LIVE, KIND_PREMATCH, MatchOdds
from .base import BaseParser

# Зеркала JSON-API линии Fonbet (структура одинаковая)
API_URLS = [
    "https://line32.bk6bba-resources.com/events/list?lang=ru&scopeMarket=1600",
    "https://line01.bk6bba-resources.com/events/list?lang=ru&scopeMarket=1600",
]

F_P1, F_DRAW, F_P2 = 921, 922, 923


class FonbetParser(BaseParser):
    name = "Fonbet"

    def fetch_odds(self) -> list[MatchOdds]:
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

        # factors сгруппированы по событию в customFactors
        result = []
        for ef in data.get("customFactors", []):
            event = events.get(ef.get("e"))
            if not event:
                continue
            factors = {f["f"]: f.get("v") for f in ef.get("factors", [])}
            if F_DRAW in factors:
                continue  # есть ничья — рынок трёхисходный, пропускаем
            k1, k2 = factors.get(F_P1), factors.get(F_P2)
            if not k1 or not k2:
                continue

            # У дочерних росписей (сет/период/карта) команды берём у родителя,
            # а название росписи добавляем к виду спорта, чтобы такие рынки
            # сопоставлялись между БК только с точно такими же рынками.
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

            result.append(MatchOdds(
                bookmaker=self.name,
                sport=sport,
                team1=team1,
                team2=team2,
                k1=float(k1),
                k2=float(k2),
                kind=kind,
                start_time=start_time,
            ))
        return result
