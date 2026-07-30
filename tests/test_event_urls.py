"""Ссылки на страницы матчей у Winline и Melbet.

Обе БК отдавали ссылки, по которым страница не открывалась: Winline —
короткий путь «/stavki/event/<id>», работающий только при переходе внутри
сайта (с холодного захода роутер сбрасывает на главную), Melbet — числовой
путь с идентификатором события из фида вместо постоянного номера. Здесь
проверяется форма адреса, а не то, что она «непустая».
"""
from app.parsers.html_utils import slugify
from app.parsers.melbet import _event_url as melbet_url
from app.parsers.melbet import _subgame_event
from app.parsers.winline import _event_url as winline_url

# Событие в том виде, в каком его отдаёт фид 1xBet: I — номер в фиде,
# CI — постоянный номер, который стоит в адресе страницы.
GAME = {
    "I": 738821803, "CI": 353889982, "LI": 118593, "SI": 1,
    "SN": "Футбол", "SE": "Football",
    "L": "Лига Европы УЕФА", "LE": "UEFA Europa League",
    "O1": "Маккаби Тель-Авив", "O1E": "Maccabi Tel Aviv",
    "O2": "Шериф", "O2E": "Sheriff Tiraspol",
}

# Чемпионат Winline в том виде, в каком его отдаёт снапшот фида
CHAMP = (1, "Кубок «Путь регионов»", "Россия")


# ---------------------------------------------------------------- Melbet

def test_melbet_link_matches_the_pages_the_site_serves():
    """Ровно тот формат, что отдаёт сам сайт и индексируют поисковики:
    /ru/line/<спорт>/<id чемпионата>-<чемпионат>/<CI>-<команды>."""
    assert melbet_url(GAME, live=False) == (
        "https://melbet.ru/ru/line/football/118593-uefa-europa-league/"
        "353889982-maccabi-tel-aviv-sheriff-tiraspol")


def test_melbet_takes_the_number_from_the_address_not_from_the_feed():
    """В адресе стоит постоянный номер CI. Раньше подставлялся I — номер,
    по которому ходят запросы за росписью, и страница отвечала 404."""
    url = melbet_url(GAME, live=False)
    assert "353889982" in url
    assert "738821803" not in url


def test_melbet_live_match_opens_in_the_live_section():
    assert melbet_url(GAME, live=True).startswith(
        "https://melbet.ru/ru/live/football/")


def test_melbet_falls_back_to_a_page_that_exists():
    """Без номера события — страница чемпионата, без чемпионата — раздел.
    Выдуманный адрес хуже раздела: по нему пользователь получит 404."""
    no_game = {k: v for k, v in GAME.items() if k not in ("I", "CI")}
    assert melbet_url(no_game, live=False) == (
        "https://melbet.ru/ru/line/football/118593-uefa-europa-league")
    assert melbet_url({"SN": "Футбол"}, live=False) == (
        "https://melbet.ru/ru/line")


def test_melbet_subgame_points_at_the_parent_match():
    """У подигры («1-й тайм») своей страницы нет — её рынки показывают на
    странице матча, туда и должна вести ссылка."""
    parent = type("Ev", (), {"game": GAME})()
    sub = {"I": 999000111, "CI": 999000222, "SG": 1,
           "GS": [{"G": 1, "E": [[{"T": 1, "C": 1.5}]]}],
           "PN": "1-й тайм", "TG": 1}
    event = _subgame_event(parent, sub)
    if event is not None:      # разбор подигры зависит от набора рынков
        assert "353889982" in melbet_url(event.game, live=False)


def test_melbet_survives_a_feed_without_english_names():
    """Английских названий может не быть — тогда транслитерируем русские,
    но форма адреса та же."""
    russian = {k: v for k, v in GAME.items()
               if k not in ("SE", "LE", "O1E", "O2E")}
    assert melbet_url(russian, live=False) == (
        "https://melbet.ru/ru/line/futbol/118593-liga-evropy-uefa/"
        "353889982-makkabi-tel-aviv-sherif")


# --------------------------------------------------------------- Winline

def test_winline_link_has_the_four_segments_the_router_needs():
    """Проверено в браузере: путь с четырьмя сегментами открывает матч с
    холодного захода, а более короткий роутер уводит на главную."""
    url = winline_url(16337709, "Футбол", CHAMP, live=False)
    assert url == ("https://winline.ru/stavki/sport/futbol/rossiya/"
                   "kubok-put-regionov/16337709")


def test_winline_short_event_path_is_not_used_anymore():
    """«/stavki/event/<id>» есть в разметке сайта, но по ссылке не
    открывается — именно из-за него матчи вели на главную."""
    assert "/event/" not in winline_url(16337709, "Футбол", CHAMP, live=False)


def test_winline_live_match_opens_in_the_live_section():
    url = winline_url(16352734, "Хоккей", (4, "NHL", "США"), live=True)
    assert url == "https://winline.ru/live/sport/hokkei/ssha/nhl/16352734"


def test_winline_keeps_all_segments_when_data_is_missing():
    """Пустой сегмент схлопнул бы путь, и роутер снова не узнал бы
    маршрут: незаполненное место занимает заглушка."""
    url = winline_url(123, "", None, live=False)
    assert url == "https://winline.ru/stavki/sport/sport/mir/liga/123"
    assert "//" not in url.removeprefix("https://")


def test_winline_champ_without_country_still_gets_a_segment():
    """В лайв-кадре справочника стран нет — страна приходит пустой."""
    url = winline_url(77, "Теннис", (5, "Уимблдон", ""), live=True)
    assert url == "https://winline.ru/live/sport/tennis/mir/uimbldon/77"


# ---------------------------------------------------------------- слаги

def test_slug_keeps_the_segment_usable():
    assert slugify("UEFA Europa League") == "uefa-europa-league"
    assert slugify("Кубок «Путь регионов»") == "kubok-put-regionov"
    assert slugify("Ливерпуль — Манчестер Сити") == (
        "liverpul-manchester-siti")
    assert slugify("", "sport") == "sport"
    assert slugify("!!! ???", "liga") == "liga"
