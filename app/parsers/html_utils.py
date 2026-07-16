"""Общие помощники для HTML-парсеров (Winline / BetBoom / Лига Ставок)."""
from ..models import MarketOdds


def num(text: str) -> float | None:
    try:
        return float(text.replace(",", ".").strip())
    except (ValueError, AttributeError):
        return None


def pair(coefs: list[str]) -> tuple[float | None, float | None]:
    if len(coefs) != 2:
        return None, None
    return num(coefs[0]), num(coefs[1])


def parse_totals(event, base: dict, coef_selector: str) -> list[MarketOdds]:
    """Достаёт двухисходные тоталы (ТБ/ТМ) с карточки события.

    Ищем блоки рынка «тотал» с параметром линии (data-param / .param) и
    парой кэфов больше/меньше. Если сайт их не размечает — пустой список.
    """
    out = []
    selectors = "[data-market='total'], .market-total, .total-row, .total"
    for block in event.select(selectors):
        pt = block.get("data-param") or block.get("data-total")
        if not pt:
            label = block.select_one(".total-value, .param, .handicap")
            pt = label.get_text(strip=True) if label else None
        coefs = [c.get_text(strip=True) for c in block.select(coef_selector)]
        over, under = pair(coefs)
        if pt and over and under:
            out.append(MarketOdds(
                market=f"Тотал {pt}", market_key=f"total:{pt}",
                outcome1=f"ТБ {pt}", outcome2=f"ТМ {pt}",
                k1=over, k2=under, **base))
    return out
