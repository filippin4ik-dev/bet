"""Fonbet — парсинг публичного JSON-API линии.

Берём только матчи с двумя исходами: события, у которых в основной
росписи есть факторы П1 (id=921) и П2 (id=923), но НЕТ ничьей (id=922).
"""
from ..models import MatchOdds
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
            if not event or event.get("level") != 1:
                continue  # только основные события, не «дочерние» росписи
            factors = {f["f"]: f.get("v") for f in ef.get("factors", [])}
            if F_DRAW in factors:
                continue  # есть ничья — рынок трёхисходный, пропускаем
            k1, k2 = factors.get(F_P1), factors.get(F_P2)
            if not k1 or not k2:
                continue
            result.append(MatchOdds(
                bookmaker=self.name,
                sport=sports.get(event.get("sportId"), "Спорт"),
                team1=event.get("team1", ""),
                team2=event.get("team2", ""),
                k1=float(k1),
                k2=float(k2),
            ))
        return result
