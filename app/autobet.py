"""Авто-ставка на найденную вилку.

Логика в двух шагах:
1. build_plan_*() — считает, сколько реально можно поставить на каждую
   ногу, исходя из ПОДКЛЮЧЁННЫХ аккаунтов и их последнего известного
   баланса (см. accounts_manager). Если для БК ноги нет подключённого
   аккаунта с известным балансом — ставка по этой ноге считается
   невозможной (stake=0), а не «на глазок».
2. execute_plan() — реально размещает (или имитирует — см. dry_run)
   ставки через коннекторы БК и пишет результат в журнал (bet_log).

ВАЖНО: реальная постановка ставки (place_bet у коннекторов, кроме Mock)
экспериментальна и не проверена на живых аккаунтах — см. предупреждения
в app/connectors/selenium_generic.py. По умолчанию (AUTOBET_DRY_RUN=1)
никакие реальные запросы на сайты БК не уходят.
"""
import logging
from dataclasses import dataclass, field

from . import accounts_manager, config, db
from .arbitrage import calc_stakes, calc_stakes3
from .connectors import BetLeg, MockConnector, get_connector

log = logging.getLogger("autobet")


@dataclass
class LegPlan:
    bookmaker: str
    outcome_label: str
    odds: float
    stake: float
    url: str | None
    limited: bool  # True — ставка урезана/невозможна из-за лимита баланса


@dataclass
class BetPlan:
    match_key: str
    kind3: bool
    sport: str
    match: str
    market: str
    profit_pct: float
    legs: list[LegPlan] = field(default_factory=list)
    note: str | None = None


def _max_bank_n(odds: list[float], caps: list[float | None]) -> float | None:
    """Максимальный общий банк, при котором КАЖДАЯ нога не превышает
    свой лимит (пропорции ставок заданы формулой calc_stakes/calc_stakes3:
    stake_i = bank * (1/odds_i) / sum(1/odds))."""
    if any(c is None for c in caps):
        return None
    s = sum(1 / o for o in odds)
    banks = []
    for o, cap in zip(odds, caps):
        frac = (1 / o) / s
        banks.append(cap / frac if frac > 0 else float("inf"))
    return min(banks) if banks else None


def build_plan_2way(arb: dict) -> BetPlan:
    balances = accounts_manager.available_balance_by_bookmaker()
    cap1 = accounts_manager.max_stake_for_bookmaker(arb["k1_bookmaker"], balances)
    cap2 = accounts_manager.max_stake_for_bookmaker(arb["k2_bookmaker"], balances)
    bank = _max_bank_n([arb["k1_max"], arb["k2_max"]], [cap1, cap2])
    note = None
    if bank is None:
        note = ("Не удалось рассчитать сумму ставки: для одной или обеих "
                "БК нет подключённого аккаунта с известным балансом "
                "(добавьте аккаунт в админке и обновите баланс).")
        stakes = {"stake1": 0.0, "stake2": 0.0}
    else:
        stakes = calc_stakes(arb["k1_max"], arb["k2_max"], bank)
    legs = [
        LegPlan(arb["k1_bookmaker"], arb["outcome1"], arb["k1_max"],
                float(stakes["stake1"]), arb.get("k1_url"), bank is None),
        LegPlan(arb["k2_bookmaker"], arb["outcome2"], arb["k2_max"],
                float(stakes["stake2"]), arb.get("k2_url"), bank is None),
    ]
    return BetPlan(match_key=arb["match_key"], kind3=False, sport=arb["sport"],
                   match=arb["match"], market=arb["market"],
                   profit_pct=arb["profit_pct"], legs=legs, note=note)


def build_plan_3way(arb: dict) -> BetPlan:
    balances = accounts_manager.available_balance_by_bookmaker()
    cap1 = accounts_manager.max_stake_for_bookmaker(arb["k1_bookmaker"], balances)
    capx = accounts_manager.max_stake_for_bookmaker(arb["kx_bookmaker"], balances)
    cap2 = accounts_manager.max_stake_for_bookmaker(arb["k2_bookmaker"], balances)
    bank = _max_bank_n([arb["k1_max"], arb["kx_max"], arb["k2_max"]],
                       [cap1, capx, cap2])
    note = None
    if bank is None:
        note = ("Не удалось рассчитать сумму ставки: не для всех трёх БК "
                "есть подключённый аккаунт с известным балансом.")
        stakes = {"stake1": 0.0, "stakex": 0.0, "stake2": 0.0}
    else:
        stakes = calc_stakes3(arb["k1_max"], arb["kx_max"], arb["k2_max"], bank)
    legs = [
        LegPlan(arb["k1_bookmaker"], arb["outcome1"], arb["k1_max"],
                float(stakes["stake1"]), arb.get("k1_url"), bank is None),
        LegPlan(arb["kx_bookmaker"], arb["outcomex"], arb["kx_max"],
                float(stakes["stakex"]), arb.get("kx_url"), bank is None),
        LegPlan(arb["k2_bookmaker"], arb["outcome2"], arb["k2_max"],
                float(stakes["stake2"]), arb.get("k2_url"), bank is None),
    ]
    return BetPlan(match_key=arb["match_key"], kind3=True, sport=arb["sport"],
                   match=arb["match"], market=arb["market"],
                   profit_pct=arb["profit_pct"], legs=legs, note=note)


def execute_plan(plan: BetPlan, dry_run: bool) -> list[dict]:
    accounts_by_bk = {a["bookmaker"]: a for a in db.list_accounts()
                      if a["enabled"]}
    results: list[dict] = []
    for leg in plan.legs:
        if leg.stake <= 0:
            results.append({
                "bookmaker": leg.bookmaker, "outcome": leg.outcome_label,
                "ok": False, "stake": 0.0, "odds": leg.odds,
                "message": ("Нет подключённого аккаунта с известным "
                           "балансом для этой БК — ставка не рассчитана."),
            })
            continue
        bet_leg = BetLeg(
            bookmaker=leg.bookmaker, outcome_label=leg.outcome_label,
            market_key="", team1=plan.match, team2="", odds=leg.odds,
            stake=leg.stake, url=leg.url)
        acc = accounts_by_bk.get(leg.bookmaker)
        use_dry = dry_run or acc is None
        if use_dry:
            connector = MockConnector(leg.bookmaker, "dry-run", "dry-run")
        else:
            creds = db.get_account_credentials(acc["id"])
            login, password = creds if creds else ("", "")
            connector = get_connector(leg.bookmaker, login, password,
                                      account_id=acc["id"],
                                      cookies=db.get_account_cookies(
                                          acc["id"]),
                                      login_type=acc.get("login_type"))
        try:
            res = connector.place_bet(bet_leg)
            results.append({
                "bookmaker": leg.bookmaker, "outcome": leg.outcome_label,
                "ok": res.ok, "stake": leg.stake, "odds": leg.odds,
                "message": res.message,
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "bookmaker": leg.bookmaker, "outcome": leg.outcome_label,
                "ok": False, "stake": leg.stake, "odds": leg.odds,
                "message": str(exc),
            })
        finally:
            connector.close()
    return results


def place_on_arb(arb: dict, kind3: bool, dry_run: bool | None = None) -> dict:
    """Полный цикл: расчёт плана + размещение + журнал. Возвращает сводку
    для ответа API/фронтенда."""
    if dry_run is None:
        dry_run = config.AUTOBET_DRY_RUN
    plan = build_plan_3way(arb) if kind3 else build_plan_2way(arb)
    results = execute_plan(plan, dry_run=dry_run)
    ok = all(r["ok"] for r in results) and bool(results)
    db.log_bet_attempt(
        match_key=plan.match_key, kind3=kind3, sport=plan.sport,
        match=plan.match, market=plan.market, profit_pct=plan.profit_pct,
        dry_run=dry_run, ok=ok, legs=results,
        error=plan.note if not ok else None)
    return {
        "ok": ok, "dry_run": dry_run, "note": plan.note, "legs": results,
    }
