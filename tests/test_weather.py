"""
Погода как источник фактов.

Сети тут нет ни в одном тесте: ответ open-meteo подкладывается вместо
`weather._get`. Настоящий запрос в тестах сделал бы прогон зависимым от
интернета - ровно того, чего мы в прототипе и избегаем.
"""
from datetime import date, timedelta

import pytest

from wam import weather
from wam.schema import DayRecord, Fact, Timeline

# Ответ open-meteo столбцами - ровно в том виде, в каком он приходит
ANSWER = {
    "daily": {
        "time": ["2026-08-17", "2026-08-18", "2026-08-19"],
        "temperature_2m_max": [22.4, 29.6, 23.6],
        "temperature_2m_min": [17.0, 18.2, 13.9],
        "pressure_msl_mean": [1013.3, 1004.1, 1005.0],
        "relative_humidity_2m_mean": [73, 65, 88],
        "cloud_cover_mean": [91, 30, 75],
    }
}

PLACE = {"results": [{"latitude": 55.75, "longitude": 37.61}]}


@pytest.fixture(autouse=True)
def clean():
    """Запомненные города и погода не должны перетекать из теста в тест."""
    weather.forget()
    yield
    weather.forget()


def _answers(*replies):
    """Подставной сервис: отдаёт заготовленные ответы по очереди."""
    queue = list(replies)

    def get(url, params, timeout):
        return queue.pop(0) if queue else None

    return get


def test_no_city_means_no_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("без города в сеть ходить незачем")

    monkeypatch.setattr(weather, "_get", boom)
    assert weather.readings("") == {}
    assert weather.readings("   ") == {}


def test_readings_unpack_columns(monkeypatch):
    monkeypatch.setattr(weather, "_get", _answers(PLACE, ANSWER))
    days = weather.readings("Москва")

    assert set(days) == {date(2026, 8, 17), date(2026, 8, 18), date(2026, 8, 19)}
    assert days[date(2026, 8, 18)]["давление"] == 1004.1
    assert days[date(2026, 8, 19)]["облачность"] == 75.0


def test_no_network_is_not_a_failure(monkeypatch):
    monkeypatch.setattr(weather, "_get", _answers())      # всё время None
    assert weather.readings("Москва") == {}


def test_unknown_city_is_remembered(monkeypatch):
    """Опечатку в названии не переспрашиваем у сервиса на каждую реплику."""
    calls = []

    def get(url, params, timeout):
        calls.append(url)
        return {"results": []}

    monkeypatch.setattr(weather, "_get", get)
    assert weather.readings("Мсоква") == {}
    assert weather.readings("Мсоква") == {}
    assert len(calls) == 1


def test_broken_network_is_not_remembered(monkeypatch):
    """А вот сбой связи запоминать нельзя: сеть вернётся, город найдётся."""
    monkeypatch.setattr(weather, "_get", _answers(None, PLACE, ANSWER))
    assert weather.readings("Москва") == {}
    assert weather.readings("Москва") != {}


def test_factors_from_one_day():
    today = {"давление": 1002.0, "температура днём": 30.0,
             "облачность": 90.0, "влажность": 88.0}
    names = {f.name: f.value for f in weather.day_factors(today, {"давление": 1012.0})}

    assert names["низкое давление"] == 1.0
    assert names["резкий перепад давления"] == 1.0
    assert names["жара"] == 1.0
    assert names["холод"] == 0.0
    assert names["пасмурно"] == 1.0
    assert names["сыро"] == 1.0
    assert all(f.source == weather.SOURCE for f in weather.day_factors(today))


def test_calm_day_gives_zeroes_not_silence():
    """Дни без погодных крайностей нужны так же, как дни с ними."""
    today = {"давление": 1014.0, "температура днём": 18.0,
             "облачность": 20.0, "влажность": 55.0}
    names = {f.name: f.value for f in weather.day_factors(today, {"давление": 1013.0})}

    assert set(names) == {"низкое давление", "резкий перепад давления",
                          "жара", "холод", "пасмурно", "сыро"}
    assert set(names.values()) == {0.0}


def test_unknown_yesterday_means_no_verdict_about_the_jump():
    """Вчерашнего давления нет - про перепад молчим, а не пишем «не было»."""
    facts = weather.day_factors({"давление": 1002.0})
    assert [f.name for f in facts] == ["низкое давление"]


def test_missing_measurement_is_not_invented():
    daily = dict(ANSWER["daily"], pressure_msl_mean=[1013.3, None, 1005.0])
    days = weather._unpack(daily)
    assert "давление" not in days[date(2026, 8, 18)]
    assert days[date(2026, 8, 18)]["температура днём"] == 29.6   # соседнее на месте


def test_attach_only_fills_days_that_exist():
    timeline = Timeline()
    timeline.add(DayRecord(day=date(2026, 8, 18)))
    weather.attach(timeline, weather._unpack(ANSWER["daily"]))

    assert len(timeline.days) == 1              # чужих дней в дневнике не завелось
    day = timeline.days[0]
    assert day.factor("жара") == 1.0
    assert day.factor("резкий перепад давления") == 1.0     # 1013,3 -> 1004,1
    assert day.factor("пасмурно") == 0.0


def test_attach_does_not_touch_what_already_written():
    timeline = Timeline()
    record = DayRecord(day=date(2026, 8, 18))
    record.add(Fact("factor", "жара", 0.0, "diary"))
    timeline.add(record)
    weather.attach(timeline, weather._unpack(ANSWER["daily"]))

    assert record.factor("жара") == 0.0         # сказанное человеком не перебиваем


def test_attach_twice_changes_nothing():
    timeline = Timeline()
    timeline.add(DayRecord(day=date(2026, 8, 18)))
    days = weather._unpack(ANSWER["daily"])
    first = weather.attach(timeline, days)

    assert first > 0
    assert weather.attach(timeline, days) == 0


def test_second_call_does_not_ask_the_service_again(monkeypatch):
    """Погода за день меняется медленно, а спрашивают её на каждую реплику."""
    calls = []

    def get(url, params, timeout):
        calls.append(url)
        return PLACE if "geocoding" in url else ANSWER

    monkeypatch.setattr(weather, "_get", get)
    weather.readings("Москва")
    weather.readings("Москва")
    assert len(calls) == 2      # геокодинг и погода, по одному разу


def test_step_without_city_does_not_go_online(monkeypatch):
    """Обычный разговор без города погоду не трогает - и в сеть не лезет."""
    from wam import dialog
    from wam.diary import DiaryStore

    def boom(*args, **kwargs):
        raise AssertionError("города нет, а в сеть пошли")

    monkeypatch.setattr(weather, "_get", boom)
    diary = DiaryStore().get("web")
    dialog.step(diary, "Пил кофе, спалось так себе", parser=dialog.Parser())
    assert diary.today().factor("кофе") == 1.0


def test_step_writes_weather_into_the_diary(monkeypatch):
    """С городом факторы погоды сами оказываются в записи за день."""
    from wam import dialog
    from wam.diary import DiaryStore

    today = date.today()
    daily = {
        "time": [(today - timedelta(days=1)).isoformat(), today.isoformat()],
        "temperature_2m_max": [14.0, 15.0],
        "temperature_2m_min": [8.0, 9.0],
        "pressure_msl_mean": [1014.0, 1002.0],
        "relative_humidity_2m_mean": [60, 62],
        "cloud_cover_mean": [40, 95],
    }
    monkeypatch.setattr(weather, "_get", _answers(PLACE, {"daily": daily}))

    diary = DiaryStore().get("web")
    diary.city = "Москва"
    dialog.step(diary, "Пил кофе, спалось так себе", parser=dialog.Parser())

    record = diary.today()
    assert record.factor("резкий перепад давления") == 1.0
    assert record.factor("пасмурно") == 1.0
    assert record.factor("жара") == 0.0


# ── погода в разговоре ────────────────────────────────────────────────────

def test_ordinary_weather_is_not_news():
    """Про обычный день говорить нечего: фактора нет - и строки нет."""
    calm = {"давление": 1014.0, "температура днём": 18.0,
            "облачность": 20.0, "влажность": 55.0}
    assert weather.day_note(calm, {"давление": 1013.0}) == ""
    assert weather.day_note({}) == ""


def test_note_names_the_number_behind_the_factor():
    note = weather.day_note({"давление": 1004.0}, {"давление": 1011.0})
    assert "на 7 гПа" in note
    assert "влияет ли на вас" in note


def test_weather_factor_is_said_in_the_feed_and_only_once(monkeypatch):
    """
    Погодный фактор молча ложился в дневник, и человек про него не знал вовсе.
    Теперь про него говорится строкой - но один раз, а не на каждую реплику.
    """
    from wam import dialog
    from wam.diary import DiaryStore

    today = date.today()
    daily = {
        "time": [(today - timedelta(days=1)).isoformat(), today.isoformat()],
        "temperature_2m_max": [14.0, 15.0],
        "temperature_2m_min": [8.0, 9.0],
        "pressure_msl_mean": [1011.0, 1004.0],
        "relative_humidity_2m_mean": [60, 62],
        "cloud_cover_mean": [40, 45],
    }
    monkeypatch.setattr(weather, "_get", _answers(PLACE, {"daily": daily}))

    diary = DiaryStore().get("web")
    diary.city = "Москва"
    said = dialog.step(diary, "Пил кофе, спалось так себе", parser=dialog.Parser())
    assert any("скакнуло на 7 гПа" in message["text"] for message in said)

    again = dialog.step(diary, "Ещё была тренировка", parser=dialog.Parser())
    assert not any("скакнуло на 7 гПа" in message["text"] for message in again)


def test_calm_weather_says_nothing_in_the_feed(monkeypatch):
    """Обычная погода в разговоре не появляется - это не новость."""
    from wam import dialog
    from wam.diary import DiaryStore

    today = date.today()
    daily = {
        "time": [(today - timedelta(days=1)).isoformat(), today.isoformat()],
        "temperature_2m_max": [18.0, 19.0],
        "temperature_2m_min": [11.0, 12.0],
        "pressure_msl_mean": [1013.0, 1014.0],
        "relative_humidity_2m_mean": [55, 57],
        "cloud_cover_mean": [30, 35],
    }
    monkeypatch.setattr(weather, "_get", _answers(PLACE, {"daily": daily}))

    diary = DiaryStore().get("web")
    diary.city = "Москва"
    said = dialog.step(diary, "Пил кофе, спалось так себе", parser=dialog.Parser())
    assert not any("влияет ли на вас" in message["text"] for message in said)
