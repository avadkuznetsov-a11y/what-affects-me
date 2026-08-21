"""
Сегодняшний день на фоне прежних: прибор против рассказа и наблюдения на
малых данных.

Статистику для наблюдений считает тот же движок, что и выводы, только с
пониженным порогом, - поэтому перестановки тут короткие (FAST_PERMUTATIONS).
"""
from datetime import date, timedelta

from conftest import FAST_PERMUTATIONS
from wam.insights import OBSERVATION_MIN_DAYS, find_links
from wam.schema import DayRecord, Fact, Timeline
from wam.today import MAX_LINES, observations

TODAY = date(2026, 8, 20)


def _day(offset: int = 0, **facts) -> DayRecord:
    """День со сказанным человеком: метрики - числами, привычки - единицами."""
    record = DayRecord(day=TODAY - timedelta(days=offset))
    for name, value in facts.items():
        name = name.replace("_", " ")
        kind = "metric" if name in ("энергия", "настроение", "тревога",
                                    "качество сна", "головная боль") else "factor"
        record.add(Fact(kind, name, float(value), "diary"))
    return record


def _ring(record: DayRecord, **facts) -> DayRecord:
    """Дописать в день показания кольца - они приходят из другого источника."""
    for name, value in facts.items():
        name = name.replace("_", " ")
        kind = "metric" if name in ("энергия", "стресс", "качество сна") else "factor"
        record.add(Fact(kind, name, float(value), "sber_ring"))
    return record


def _timeline(*days: DayRecord) -> Timeline:
    line = Timeline()
    for day in days:
        line.add(day)
    return line


# ── прибор против рассказа ────────────────────────────────────────────────

def test_device_disagrees_with_the_story():
    today = _ring(_day(настроение=8), высокий_стресс=1, высокий_пульс=1)
    lines = observations(_timeline(today), today, set())

    assert len(lines) == 1
    assert "8 из 10" in lines[0]
    assert "кольцо показало высокий стресс и пульс выше обычного" in lines[0]


def test_calm_day_against_a_tense_ring():
    """Тот самый случай: человек написал «спокойный день», а кольцо не согласно."""
    today = _ring(_day(тревога=9), высокий_стресс=1)
    lines = observations(_timeline(today), today, set())

    assert lines[0].startswith("Вы описали день как спокойный, "
                               "а кольцо показало высокий стресс")
    # Шкала тревоги внутри программы перевёрнута - цифру наружу не выносим
    assert "9" not in lines[0]


def test_device_disagreement_is_said_from_the_first_day():
    """История для этого не нужна: два взгляда на один и тот же день."""
    today = _ring(_day(энергия=8), высокий_стресс=1)
    assert observations(_timeline(today), today, set())


def test_device_explains_the_feeling():
    today = _ring(_day(энергия=3), мало_спал=1)
    lines = observations(_timeline(today), today, set())

    assert "короткий сон" in lines[0]
    assert "совпадение в один день" in lines[0]      # причину не назначаем


def test_device_agrees_shortly():
    today = _ring(_day(энергия=8), хорошо_выспался=1)
    lines = observations(_timeline(today), today, set())

    assert "кольцо согласно" in lines[0]


def test_device_is_not_compared_with_itself():
    """Оценку, которую поставил сам прибор, спорить с прибором не заставляем."""
    today = _ring(DayRecord(day=TODAY), энергия=8, высокий_стресс=1)
    assert observations(_timeline(today), today, set()) == []


def test_nothing_to_say_when_device_is_silent():
    today = _day(энергия=8)
    assert observations(_timeline(today), today, set()) == []


# ── наблюдения на малых данных ────────────────────────────────────────────

def _meetings_diary() -> Timeline:
    """
    Четыре дня со встречами и четыре без. Для `find_links` с обычным порогом
    в семь дней тут нет ничего, а для слоя наблюдений уже есть.

    Дни идут подряд, а не через один: при чередовании тот же разрыв виден и с
    задержкой в день, и проверка ловила бы не то, что описывает.
    """
    days = []
    for offset, energy in ((8, 8.0), (7, 9.0), (6, 8.0), (5, 7.0)):
        days.append(_day(offset, много_встреч=1, энергия=energy))
    for offset, energy in ((4, 5.0), (3, 4.0), (2, 5.0), (1, 6.0)):
        days.append(_day(offset, много_встреч=0, энергия=energy))
    return _timeline(*days)


def _hints(timeline: Timeline):
    return find_links(timeline, permutations=FAST_PERMUTATIONS,
                      min_days=OBSERVATION_MIN_DAYS)


def test_small_data_gives_nothing_to_the_usual_engine():
    assert find_links(_meetings_diary(), permutations=FAST_PERMUTATIONS) == []


def test_observation_names_both_averages_and_calls_itself_an_observation():
    timeline = _meetings_diary()
    hints = _hints(timeline)
    today = _day(много_встреч=1)
    timeline.add(today)

    lines = observations(timeline, today, {"много встреч"}, hints)
    said = next(line for line in lines if "Замечено" in line)

    assert "больше сил" in said
    assert "8,0 против 5,0" in said       # средние по группам, а не разница
    assert said.endswith("Пока это наблюдение, не вывод.")


def test_observation_only_about_what_was_named_now():
    timeline = _meetings_diary()
    hints = _hints(timeline)
    today = _day(прогулка=1)
    timeline.add(today)

    lines = observations(timeline, today, {"прогулка"}, hints)
    assert not any("Замечено" in line for line in lines)


def _beer_diary() -> Timeline:
    """
    Пиво вечером - и назавтра голова. Связь именно с завтрашним днём: в сам
    день выпивки голова не болит, в тот же день тут искать нечего.

    Шкала как везде в программе: больше - лучше, поэтому сильная головная боль
    это низкая оценка.
    """
    days = [_day(offset, алкоголь=1, головная_боль=7.0) for offset in (9, 6, 3)]
    days += [_day(offset, алкоголь=0, головная_боль=2.0) for offset in (8, 5, 2)]
    days += [_day(offset, алкоголь=0, головная_боль=7.0) for offset in (10, 7, 4, 1)]
    return _timeline(*days)


def test_observation_can_point_at_tomorrow():
    timeline = _beer_diary()
    hints = _hints(timeline)
    today = _day(алкоголь=1)
    timeline.add(today)

    lines = observations(timeline, today, {"алкоголь"}, hints)
    said = next(line for line in lines if "Замечено" in line)

    assert "на следующий день" in said
    assert "голова болит чаще" in said
    assert "«алкоголь» уже" in said       # сколько раз это уже было
    assert "Пока это наблюдение, не вывод." in said


def test_observation_does_not_repeat_the_habit_line_twice():
    timeline = _beer_diary()
    today = _day(алкоголь=1)
    timeline.add(today)

    lines = observations(timeline, today, {"алкоголь"}, _hints(timeline))
    assert sum(line.count("«алкоголь» уже") for line in lines) == 1


def test_never_more_than_two_lines():
    timeline = _meetings_diary()
    hints = _hints(timeline)
    today = _ring(_day(много_встреч=1, прогулка=1, энергия=9.0), высокий_стресс=1)
    timeline.add(today)
    for offset in (11, 13, 15):
        timeline.add(_day(offset, прогулка=1, энергия=6.0))

    assert len(observations(timeline, today, {"много встреч", "прогулка"}, hints)) <= MAX_LINES


def test_without_hints_the_old_behaviour_stays():
    """Наблюдений нет - остаются сравнение с обычным и счёт привычки."""
    timeline = _meetings_diary()
    today = _day(много_встреч=1, энергия=9.0)
    timeline.add(today)

    lines = observations(timeline, today, {"много встреч"})
    assert any("сил больше обычного" in line for line in lines)
    assert not any("Замечено" in line for line in lines)
