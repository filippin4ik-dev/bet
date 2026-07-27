"""Тесты разбора «экзотики» (src=16) Winline — is_decodable_exotic и
WinlineParser._exotic()/_market(). Раньше рынки src=16 ловились жёстким
белым списком конкретных текстов (~2% типов линий), теперь — семантически
по слову тотал/фора/чет-нечет + подписям исходов (см. wl_feed.py и
winline.py). Тесты гоняются офлайн, без сети — прямо на синтетических
tipline/line словарях, как их отдаёт wl_feed.py.
"""
from app.parsers.wl_feed import is_decodable_exotic
from app.parsers.winline import WinlineParser


def _tl(text, r0="Больше", r1="Меньше", src=16, count_v=2):
    r = [r0, r1] + [""] * 28
    return {"id": abs(hash(text)) % 10_000_000, "src": src, "text": text,
            "R": r, "countV": count_v}


BASE = dict(bookmaker="Winline", sport="Футбол", team1="Спартак",
            team2="Зенит", kind="prematch", start_time=None,
            start_ts=None, url="https://winline.ru/stavki/event/1")


def _market(ln_v, tl, koef="2.5", fav=0, sport_info=None):
    p = WinlineParser()
    ln = {"id": 1, "event": 1, "tid": tl["id"], "fav": fav, "koef": koef,
          "v": ln_v}
    return p._market(ln, tl, sport_info or {}, dict(BASE))


# ---------- is_decodable_exotic ----------

def test_decodable_exotic_accepts_any_subject_total():
    """Раньше ловилось только «Тотал [a] (@NP@) @[12]» (инд. тотал) — общий
    «Тотал угловых [a] (@NP@)»/«Тотал [a] (ауты) (@NP@)» терялся полностью."""
    for text in ("Тотал угловых [a] (@NP@)", "Тотал [a] (ауты) (@NP@)",
                 "Тотал [a] (офсайды) (@NP@)", "Тотал карточек [a] (@NP@)",
                 "Тотал [a] эйсов (@NP@)", "@2HT@ тотал [a] (без @OT@)"):
        assert is_decodable_exotic(_tl(text, "Больше", "Меньше")), text


def test_decodable_exotic_accepts_any_subject_hcap():
    for text in ("Фора угловых [a] (@NP@)", "Фора [a] (ауты) (@NP@)",
                 "Фора [a] (фолы) (@NP@)"):
        assert is_decodable_exotic(_tl(text, "1", "2")), text


def test_decodable_exotic_accepts_any_subject_oddeven():
    for text in ("Чет/Нечет карточек (@NP@)", "Чет/Нечет угловые (@NP@)",
                 "Чет/Нечет (@PEN@)", "@2HT@ чет/нечет (без @OT@)"):
        assert is_decodable_exotic(_tl(text, "Нечет", "Чет")), text


def test_decodable_exotic_rejects_multi_outcome_markets():
    """«Точный счёт»/«Мультисчёт»/«Двойной шанс» — 3+ исходов, подписи не
    совпадают ни с одним из трёх двухисходных шаблонов, countV тоже не 2."""
    assert not is_decodable_exotic(
        _tl("Точный счет", "5:0", "5:1", count_v=10))
    assert not is_decodable_exotic(
        _tl("Мультисчет", "1:0, 2:0, 3:0", "0:1, 0:2, 0:3", count_v=11))
    assert not is_decodable_exotic(
        _tl("Двойной шанс (@NP@)", "1X", "12", count_v=3))


def test_decodable_exotic_rejects_unrelated_text():
    assert not is_decodable_exotic(_tl("Кто убьет больше баронов (@NP@)",
                                       "Команда 1", "Команда 2"))
    assert not is_decodable_exotic(_tl("Метод победы", "КО", "Судьи"))


def test_decodable_exotic_ignores_non_exotic_src():
    tl = _tl("Тотал [a] (@NP@)", "Больше", "Меньше")
    tl["src"] = 4
    assert not is_decodable_exotic(tl)


def test_decodable_exotic_wrong_labels_rejected():
    """Тот же текст, но справочник вдруг подписал исходы иначе — не
    разбираем (сменилась семантика типа линии)."""
    assert not is_decodable_exotic(
        _tl("Тотал угловых [a] (@NP@)", "Команда 1", "Команда 2"))


# ---------- WinlineParser._exotic() через _market() ----------

def test_market_general_subject_total():
    tl = _tl("Тотал угловых [a] (@NP@)", "Больше", "Меньше")
    o = _market([1.85, 1.95], tl, koef="9.5")
    assert o is not None
    assert o.market_key == "total:corners:9.5"
    assert o.outcome1 == "ТБ 9.5" and o.outcome2 == "ТМ 9.5"
    assert (o.k1, o.k2) == (1.85, 1.95)


def test_market_general_subject_hcap():
    tl = _tl("Фора угловых [a] (@NP@)", "1", "2")
    o = _market([1.9, 1.9], tl, koef="1.5", fav=1)
    assert o is not None
    assert o.market_key == "hcap:corners:-1.5"
    assert o.outcome1 == "Ф1 -1.5" and o.outcome2 == "Ф2 +1.5"


def test_market_general_subject_oddeven():
    tl = _tl("Чет/Нечет карточек (@NP@)", "Нечет", "Чет")
    o = _market([1.9, 1.9], tl)
    assert o is not None
    assert o.market_key == "oddeven:cards"
    assert o.outcome1 == "Чет" and o.outcome2 == "Нечет"
    # порядок кэфов в фиде [Нечет, Чет] -> ("Чет","Нечет") в MarketOdds
    assert (o.k1, o.k2) == (1.9, 1.9)


def test_market_individual_total_side_anywhere_in_text():
    """Индивидуальный маркер «@1»/«@2» может стоять НЕ только в конце
    текста («Тотал [a] @1 (ауты) (@NP@)»), а перед предметом рынка."""
    tl = _tl("Тотал [a] @1 (ауты) (@NP@)", "Больше", "Меньше")
    o = _market([1.85, 1.95], tl, koef="1.5")
    assert o is not None
    assert o.market_key == "itotal:1:throwins:1.5"
    assert "Спартак" in o.market


def test_market_individual_total_side_at_end():
    tl = _tl("Тотал [a] (@NP@) @2", "Больше", "Меньше")
    o = _market([1.85, 1.95], tl, koef="1.5")
    assert o is not None
    assert o.market_key == "itotal:2::1.5"


def test_market_period_placeholder_not_mistaken_for_side():
    """«@1HT@»/«@2HT@» — период (1-й/2-й тайм), а не сторона команды: не
    должны попадать в индивидуальный тотал."""
    tl = _tl("@1HT@ тотал [a] (ауты)", "Больше", "Меньше")
    o = _market([1.85, 1.95], tl, koef="4.5")
    assert o is not None
    assert not o.market_key.startswith("itotal")
    assert o.market_key.startswith("total:")


def test_market_individual_oddeven_not_supported_but_no_crash():
    """Индивидуальный чет/нечет команды («Чет/Нечет (@NP@) @1») — модель
    не поддерживает исход, привязанный и к стороне И к чет/нечет
    одновременно; парсер должен безопасно пропустить его, а не создать
    рынок с перепутанной семантикой."""
    tl = _tl("Чет/Нечет (@NP@) @1", "Нечет", "Чет")
    o = _market([1.9, 1.9], tl)
    assert o is None


def test_market_multi_outcome_still_rejected():
    tl = _tl("Точный счет", "5:0", "5:1", count_v=10)
    o = _market([2.0, 3.0], tl)
    assert o is None


def test_market_wrong_vector_length_rejected():
    tl = _tl("Тотал угловых [a] (@NP@)", "Больше", "Меньше")
    o = _market([1.85, 1.95, 1.5], tl, koef="9.5")
    assert o is None


# ---------- @1P@/@2P@/@1OT@/@2OT@ — период с номером ПРЯМО в тексте ----------
# Раньше эти плейсхолдеры не распознавались вообще (только @1HT@/@2HT@/
# @[a]P@) и молча вырезались как «период по умолчанию» — фора/тотал
# конкретного периода теряли метку периода и ЛОЖНО сшивались с тем же
# рынком всего матча (see PR с фиксом _NUM_PERIOD_RE/_NUM_OT_RE).

def test_market_numeric_period_placeholder_hcap_differs_from_main():
    """«Фора угловых 1-го периода» и «Фора угловых» (весь матч) — РАЗНЫЕ
    рынки, market_key не должен совпадать."""
    tl_main = _tl("Фора угловых [a] (@NP@)", "1", "2")
    tl_period1 = _tl("Фора угловых [a] (@1P@)", "1", "2")
    o_main = _market([1.9, 1.9], tl_main, koef="1.5", fav=1)
    o_period1 = _market([1.9, 1.9], tl_period1, koef="1.5", fav=1)
    assert o_main is not None and o_period1 is not None
    assert o_main.market_key != o_period1.market_key
    assert "period1" in o_period1.market_key
    assert "1-й период" in o_period1.market


def test_market_numeric_period_placeholder_second_period():
    tl = _tl("Тотал угловых [a] (@2P@)", "Больше", "Меньше")
    o = _market([1.85, 1.95], tl, koef="4.5")
    assert o is not None
    assert "period2" in o.market_key
    assert "2-й период" in o.market


def test_market_overtime_placeholder_differs_from_main():
    tl_main = _tl("Фора угловых [a] (@NP@)", "1", "2")
    tl_ot = _tl("Фора угловых [a] (@1OT@)", "1", "2")
    o_main = _market([1.9, 1.9], tl_main, koef="1.5", fav=1)
    o_ot = _market([1.9, 1.9], tl_ot, koef="1.5", fav=1)
    assert o_main is not None and o_ot is not None
    assert o_main.market_key != o_ot.market_key
    assert "ot1" in o_ot.market_key
    assert "овертайм" in o_ot.market


if __name__ == "__main__":
    test_decodable_exotic_accepts_any_subject_total()
    test_decodable_exotic_accepts_any_subject_hcap()
    test_decodable_exotic_accepts_any_subject_oddeven()
    test_decodable_exotic_rejects_multi_outcome_markets()
    test_decodable_exotic_rejects_unrelated_text()
    test_decodable_exotic_ignores_non_exotic_src()
    test_decodable_exotic_wrong_labels_rejected()
    test_market_general_subject_total()
    test_market_general_subject_hcap()
    test_market_general_subject_oddeven()
    test_market_individual_total_side_anywhere_in_text()
    test_market_individual_total_side_at_end()
    test_market_period_placeholder_not_mistaken_for_side()
    test_market_individual_oddeven_not_supported_but_no_crash()
    test_market_multi_outcome_still_rejected()
    test_market_wrong_vector_length_rejected()
    test_market_numeric_period_placeholder_hcap_differs_from_main()
    test_market_numeric_period_placeholder_second_period()
    test_market_overtime_placeholder_differs_from_main()
    print("OK: all winline exotic tests passed")
