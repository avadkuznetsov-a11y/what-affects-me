"""
Дни без привычки: без них главная функция продукта не работает на словах
человека, поэтому здесь проверяется и сам факт достройки, и её границы.
"""
import random
from datetime import date, timedelta

from conftest import FAST_PERMUTATIONS
from wam.habits import (SOURCE, WINDOW_DAYS, imply_absences, missed_days,
                        told_habits)
from wam.insights import find_links
from wam.schema import DayRecord, Fact, Timeline

TODAY = date(2026, 8, 20)


def _day(offset: int, facts: list[Fact]) -> DayRecord:
    """День на offset суток назад от TODAY с готовыми фактами."""
    record = DayRecord(day=TODAY - timedelta(days=offset))
    for fact in facts:
        record.add(fact)
    return record


def _timeline(*records: DayRecord) -> Timeline:
    timeline = Timeline()
    for record in records:
        timeline.add(record)
    return timeline


def test_habit_told_once_gets_zeros_on_the_other_days():
    """Ради этого модуль и написан: у привычки должны появиться дни «без неё»."""
    timeline = _timeline(
        _day(3, [Fact("factor", "пиво", 1.0, "diary"), Fact("metric", "энергия", 4.0, "diary")]),
        _day(2, [Fact("metric", "энергия", 7.0, "diary")]),
        _day(1, [Fact("metric", "энергия", 8.0, "diary")]),
    )
    assert imply_absences(timeline, TODAY) == 2

    series = timeline.series("factor", "пиво")
    assert len(series) == 3                          # про каждый день теперь что-то известно
    assert sorted(set(series.values())) == [0.0, 1.0]


def test_zeros_are_marked_as_our_guess():
    """Ноль по молчанию - догадка программы, и по факту это должно быть видно."""
    timeline = _timeline(
        _day(2, [Fact("factor", "пиво", 1.0, "diary")]),
        _day(1, [Fact("metric", "энергия", 8.0, "diary")]),
    )
    imply_absences(timeline, TODAY)

    guessed = [f for f in timeline.days[1].facts if f.name == "пиво"]
    assert guessed[0].value == 0.0
    assert guessed[0].source == SOURCE != "diary"


def test_day_without_records_stays_empty():
    """В день, когда человек молчал, мы про него не знаем ничего."""
    timeline = _timeline(
        _day(3, [Fact("factor", "пиво", 1.0, "diary")]),
        # Погода приходит сама - записью человека это не является
        _day(2, [Fact("factor", "жара", 1.0, "weather")]),
    )
    imply_absences(timeline, TODAY)
    assert timeline.days[1].factor("пиво") is None


def test_only_habits_the_person_named_himself():
    """Весь словарь привычек сюда тащить нельзя - это выдуманные данные."""
    timeline = _timeline(
        _day(2, [Fact("factor", "пиво", 1.0, "diary")]),
        _day(1, [Fact("metric", "энергия", 8.0, "diary")]),
    )
    imply_absences(timeline, TODAY)
    assert timeline.days[1].factor("перелёт") is None
    assert timeline.days[1].names("factor") == {"пиво"}


def test_far_past_is_not_rewritten():
    """Привычка, которую не называли полтора месяца, из жизни человека ушла."""
    timeline = _timeline(
        _day(WINDOW_DAYS + 10, [Fact("factor", "пиво", 1.0, "diary")]),
        _day(WINDOW_DAYS + 9, [Fact("metric", "энергия", 8.0, "diary")]),
        _day(1, [Fact("metric", "энергия", 8.0, "diary")]),
    )
    assert imply_absences(timeline, TODAY) == 0
    assert told_habits(timeline, TODAY - timedelta(days=WINDOW_DAYS), TODAY) == set()


def test_what_the_person_said_is_not_overwritten():
    """Честный ноль со слов человека («не пил») остаётся его нулём."""
    timeline = _timeline(
        _day(2, [Fact("factor", "кофе", 1.0, "diary")]),
        _day(1, [Fact("factor", "кофе", 0.0, "diary")]),
    )
    imply_absences(timeline, TODAY)
    kept = [f for f in timeline.days[1].facts if f.name == "кофе"][0]
    assert kept.source == "diary"


def test_link_by_a_habit_from_the_story_is_found():
    """
    Главная проверка. До достройки у привычки одни единицы, группы «без неё»
    нет вовсе и движок не находит ничего - ни с каким порогом.
    """
    rng = random.Random(7)
    timeline = Timeline()
    for back in range(28, 0, -1):
        record = DayRecord(day=TODAY - timedelta(days=back))
        beer = rng.random() < 0.45
        if beer:
            record.add(Fact("factor", "пиво", 1.0, "diary"))
        sleep = 7.5 - (2.0 if beer else 0.0) + rng.gauss(0, 0.7)
        record.add(Fact("metric", "качество сна", round(max(0.0, min(10.0, sleep)), 1), "diary"))
        timeline.add(record)

    assert find_links(timeline, permutations=FAST_PERMUTATIONS) == []

    imply_absences(timeline, TODAY)
    links = find_links(timeline, permutations=FAST_PERMUTATIONS)
    beer_link = next(l for l in links if l.factor == "пиво" and l.metric == "качество сна")
    assert beer_link.effect < 0
    assert beer_link.days_without >= 7
    # Половина сравнения держится на догадке - человеку про это надо сказать
    assert beer_link.implied_without


# ── дни, когда человек не писал вовсе ─────────────────────────────────────

def test_missed_days_are_the_gap_between_last_record_and_today():
    timeline = _timeline(
        _day(3, [Fact("factor", "кофе", 1.0, "diary")]),
    )
    assert missed_days(timeline, TODAY) == [TODAY - timedelta(days=2),
                                            TODAY - timedelta(days=1)]


def test_writing_yesterday_is_not_a_gap():
    timeline = _timeline(_day(1, [Fact("factor", "кофе", 1.0, "diary")]))
    assert missed_days(timeline, TODAY) == []


def test_returning_after_a_month_is_not_a_gap_either():
    """Спрашивать про каждый день из тридцати бессмысленно - это уже не пропуск."""
    timeline = _timeline(_day(40, [Fact("factor", "кофе", 1.0, "diary")]))
    assert missed_days(timeline, TODAY) == []


def test_empty_diary_has_no_gap():
    assert missed_days(Timeline(), TODAY) == []
