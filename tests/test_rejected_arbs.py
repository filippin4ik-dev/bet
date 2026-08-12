"""Тесты отсева: кандидат, который вилкой ВЫГЛЯДИТ, но ею не является.

Две разные истории, сведённые в один список «Отсеянные»:

1. Счёт раундов в единоборствах. Одинаковая на вид линия «Тотал 2.5»
   считается у БК по РАЗНЫМ правилам: BetBoom засчитывает раунд, только
   если с его начала прошло не меньше половины времени раунда, Fonbet —
   «просто тотал». Бой, остановленный на первой минуте третьего раунда, у
   одной БК — 2 раунда, у другой — 3, и ТБ у первой против ТМ у второй
   проигрывают ОБА. Показывать такую пару вилкой нельзя.

2. Потолок доходности (ARB_MAX_PROFIT). Он и раньше отбрасывал кандидатов
   молча — теперь они не пропадают, а уходят в тот же список с причиной.

Запуск: python3 -m pytest tests/test_rejected_arbs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.arbitrage import (REJECT_COMBAT_ROUNDS,  # noqa: E402
                           REJECT_MAX_PROFIT, find_arbs, find_arbs_1x2)
from app.models import KIND_PREMATCH, MarketOdds  # noqa: E402
from app.parsers.html_utils import key_scope  # noqa: E402

NOW = 2_000_000_000.0
COMBAT = "Единоборства · UFC 300"


def mk(bookmaker, market_key, k1, k2, outcome1="ТБ 2.5", outcome2="ТМ 2.5",
       team1="Джонс", team2="Миочич", sport=COMBAT, start_ts=NOW):
    return MarketOdds(
        bookmaker=bookmaker, sport=sport, team1=team1, team2=team2,
        market=market_key, market_key=market_key,
        outcome1=outcome1, outcome2=outcome2, k1=k1, k2=k2,
        kind=KIND_PREMATCH, start_time="01.01 20:00", start_ts=start_ts)


def _rounds_pair(market_key="total:2.5", sport=COMBAT, **kw):
    """Пара кэфов BetBoom/Fonbet, дающая вилку ~5 % на счёте раундов."""
    return [mk("BetBoom", market_key, 2.10, 1.80, sport=sport, **kw),
            mk("Fonbet", market_key, 1.80, 2.10, sport=sport, **kw)]


# ---------- единоборства: раунды считаются по разным правилам ----------

def test_combat_round_total_is_not_an_arb_between_different_rules():
    """Тотал раундов BetBoom против тотала раундов Fonbet — не вилка:
    правила подсчёта раундов у них разные, и обе ноги могут проиграть."""
    rejected = []
    arbs = find_arbs(_rounds_pair(), rejected=rejected)
    assert arbs == [], "тотал раундов между разными правилами — не вилка"
    assert len(rejected) == 1
    assert rejected[0].reject_code == REJECT_COMBAT_ROUNDS
    assert "раунды" in rejected[0].reject_reason
    print("OK: test_combat_round_total_is_not_an_arb_between_different_rules")


def test_rejected_candidate_keeps_everything_needed_to_check_it_by_hand():
    """Отсеянную строку разбирают руками — значит в ней должно быть всё:
    матч, рынок, обе ноги с кэфами и БК, доходность и причина словами."""
    rejected = []
    find_arbs(_rounds_pair(), rejected=rejected)
    row = rejected[0].to_dict()
    assert row["match"] == "Джонс — Миочич"
    assert row["market"] == "Тотал 2.5"
    assert {row["k1_bookmaker"], row["k2_bookmaker"]} == {"BetBoom", "Fonbet"}
    assert row["profit_pct"] > 0
    assert row["reject_code"] == REJECT_COMBAT_ROUNDS
    assert "BetBoom" in row["reject_reason"] and "Fonbet" in row["reject_reason"]
    print("OK: test_rejected_candidate_keeps_everything_needed_to_check_it_by_hand")


def test_same_rule_bookmakers_still_make_a_round_total_arb():
    """Обратная граница: БК с ОДИНАКОВЫМ правилом (обе считают раунд с
    начала) сшиваются по тоталу раундов как раньше. Правило отсекает
    конкретное расхождение, а не рынок целиком."""
    odds = [mk("Fonbet", "total:2.5", 2.10, 1.80),
            mk("Winline", "total:2.5", 1.80, 2.10)]
    rejected = []
    arbs = find_arbs(odds, rejected=rejected)
    assert len(arbs) == 1, "у БК с одним правилом тотал раундов — вилка"
    assert rejected == []
    print("OK: test_same_rule_bookmakers_still_make_a_round_total_arb")


def test_fight_winner_is_not_affected_by_round_counting():
    """Победитель боя от правил подсчёта раундов не зависит: кто победил,
    тот победил. Такие вилки с BetBoom обязаны остаться."""
    odds = [mk("BetBoom", "winner", 2.10, 1.80, outcome1="П1", outcome2="П2"),
            mk("Fonbet", "winner", 1.80, 2.10, outcome1="П1", outcome2="П2")]
    rejected = []
    assert len(find_arbs(odds, rejected=rejected)) == 1
    assert rejected == []
    print("OK: test_fight_winner_is_not_affected_by_round_counting")


def test_round_rules_apply_only_to_combat_sports():
    """В футболе «Тотал 2.5» — это голы, а не раунды: правило единоборств
    туда не лезет, иначе BetBoom выпала бы из самого ходового рынка."""
    rejected = []
    arbs = find_arbs(_rounds_pair(sport="Футбол · РПЛ"), rejected=rejected)
    assert len(arbs) == 1 and rejected == []
    print("OK: test_round_rules_apply_only_to_combat_sports")


def test_combat_markets_that_do_not_count_rounds_are_left_alone():
    """В единоборствах считают не только раунды: тотал ударов, тотал
    тейкдаунов и рынки отдельного раунда — другие единицы счёта, правило
    подсчёта раундов на них не распространяется."""
    for key in ("total:shots:120.5", "total:round1:0.5",
                "winner:round2", "hcap:round1:-1.5"):
        rejected = []
        arbs = find_arbs(_rounds_pair(market_key=key), rejected=rejected)
        assert len(arbs) == 1, f"{key}: этот рынок раунды не считает"
        assert rejected == [], f"{key}: отсев сюда не относится"
    print("OK: test_combat_markets_that_do_not_count_rounds_are_left_alone")


def test_every_round_counting_market_is_covered():
    """Раунды считает не только тотал: фора по раундам и чет/нечет раундов
    расходятся у тех же БК ровно так же."""
    for key, out1, out2 in (("total:2.5", "ТБ 2.5", "ТМ 2.5"),
                            ("hcap::-1.5", "Ф1 -1.5", "Ф2 +1.5"),
                            ("oddeven", "Чет", "Нечет"),
                            ("itotal:1::1.5", "ИТБ1 1.5", "ИТМ1 1.5")):
        rejected = []
        arbs = find_arbs(_rounds_pair(market_key=key, outcome1=out1,
                                      outcome2=out2), rejected=rejected)
        assert arbs == [], f"{key}: раунды считаются по-разному"
        assert [a.reject_code for a in rejected] == [REJECT_COMBAT_ROUNDS]
    print("OK: test_every_round_counting_market_is_covered")


def test_key_scope_finds_the_scope_wherever_it_lives():
    """Место scope в ключе зависит от вида рынка — на этом и держится
    «рынок всего боя» против «рынка отрезка боя»."""
    assert key_scope("total:2.5") == ""
    assert key_scope("total:corners:9.5") == "corners"
    assert key_scope("hcap::-1.5") == ""
    assert key_scope("hcap:half1:-1.5") == "half1"
    assert key_scope("itotal:1::2.5") == ""
    assert key_scope("itotal:2:half1:1.5") == "half1"
    assert key_scope("winner") == ""
    assert key_scope("winner:round2") == "round2"
    print("OK: test_key_scope_finds_the_scope_wherever_it_lives")


# ---------- потолок доходности: отсев больше не молчаливый ----------

def _fat(bookmaker, k1, k2):
    return mk(bookmaker, "winner", k1, k2, outcome1="П1", outcome2="П2",
              sport="Футбол · РПЛ", team1="Спартак", team2="Зенит")


def test_capped_candidate_lands_in_the_rejected_list():
    """Кандидат выше ARB_MAX_PROFIT в основной список не попадает (это
    почти всегда ошибка сопоставления), но и не исчезает бесследно."""
    odds = [_fat("Winline", 5.00, 1.10), _fat("Fonbet", 1.10, 5.00)]
    rejected = []
    arbs = find_arbs(odds, max_profit=25, rejected=rejected)
    assert arbs == []
    assert [a.reject_code for a in rejected] == [REJECT_MAX_PROFIT]
    assert rejected[0].profit_pct > 25
    print("OK: test_capped_candidate_lands_in_the_rejected_list")


def test_rejected_list_is_optional():
    """Без списка движок ведёт себя ровно как раньше — отсеянное просто
    не попадает в результат (так его зовёт диагностика и старые тесты)."""
    odds = [_fat("Winline", 5.00, 1.10), _fat("Fonbet", 1.10, 5.00)]
    assert find_arbs(odds, max_profit=25) == []
    assert len(find_arbs(odds, max_profit=float("inf"))) == 1
    assert find_arbs(_rounds_pair()) == []
    print("OK: test_rejected_list_is_optional")


def test_1x2_cap_is_recorded_too():
    """Трёхисходный движок отсеивает по тому же потолку — и так же
    отдаёт отсеянное для ручного разбора."""
    def w(bk, k1, k2, k3):
        return MarketOdds(
            bookmaker=bk, sport="Футбол · РПЛ", team1="Спартак",
            team2="Зенит", market="Исход", market_key="winner1x2",
            outcome1="П1", outcome2="П2", outcome3="X", k1=k1, k2=k2, k3=k3,
            kind=KIND_PREMATCH, start_ts=NOW, start_time="01.01 20:00")

    odds = [w("Winline", 9.00, 2.20, 3.40), w("Fonbet", 2.60, 9.00, 9.50)]
    rejected = []
    arbs = find_arbs_1x2(odds, max_profit=25, rejected=rejected)
    assert all(a.profit_pct <= 25 for a in arbs), "потолок должен работать"
    assert rejected, "отброшенные потолком тройки должны сохраняться"
    assert all(a.reject_code == REJECT_MAX_PROFIT and a.profit_pct > 25
               and a.to_dict()["reject_reason"] for a in rejected)
    print("OK: test_1x2_cap_is_recorded_too")


def test_shown_arbs_carry_no_reject_reason():
    """У показанной вилки причины отсева быть не должно — иначе вкладка
    «Отсеянные» и основная таблица начнут показывать одно и то же."""
    odds = [_fat("Winline", 2.10, 1.80), _fat("Fonbet", 1.80, 2.10)]
    arb = find_arbs(odds)[0]
    assert arb.reject_code is None and arb.reject_reason is None
    assert arb.to_dict()["reject_code"] is None
    print("OK: test_shown_arbs_carry_no_reject_reason")


if __name__ == "__main__":
    test_combat_round_total_is_not_an_arb_between_different_rules()
    test_rejected_candidate_keeps_everything_needed_to_check_it_by_hand()
    test_same_rule_bookmakers_still_make_a_round_total_arb()
    test_fight_winner_is_not_affected_by_round_counting()
    test_round_rules_apply_only_to_combat_sports()
    test_combat_markets_that_do_not_count_rounds_are_left_alone()
    test_every_round_counting_market_is_covered()
    test_key_scope_finds_the_scope_wherever_it_lives()
    test_capped_candidate_lands_in_the_rejected_list()
    test_rejected_list_is_optional()
    test_1x2_cap_is_recorded_too()
    test_shown_arbs_carry_no_reject_reason()
    print("Все тесты прошли.")
