"""Движок связей: находит настоящее и молчит про случайное."""
from demo.generate import build
from conftest import FAST_PERMUTATIONS
from wam.insights import (MIN_DAYS_PER_GROUP, OBSERVATION_MIN_DAYS, find_links,
                          summarise)
from wam.schema import DayRecord, Fact, Timeline
from datetime import date, timedelta


def test_finds_planted_link():
    links = find_links(build(days=90), permutations=FAST_PERMUTATIONS)
    coffee = next(l for l in links if l.factor == "кофе" and l.metric == "качество сна")
    assert coffee.lag_days == 1          # влияет на следующий день
    assert coffee.effect < 0             # делает сон хуже
    assert coffee.strength == "подтверждено"


def test_placebo_factor_is_not_confirmed():
    """«Сладкое» ни на что не влияет — движок не должен объявлять связь."""
    links = find_links(build(days=90), permutations=FAST_PERMUTATIONS)
    for link in links:
        if link.factor == "сладкое":
            assert link.strength == "наблюдение"


def test_too_little_data_gives_nothing():
    timeline = Timeline()
    for offset in range(MIN_DAYS_PER_GROUP):
        day = date(2026, 8, 1) + timedelta(days=offset)
        record = DayRecord(day=day)
        record.add(Fact("factor", "кофе", 1.0))
        record.add(Fact("metric", "энергия", 5.0))
        timeline.add(record)
    assert find_links(timeline, permutations=FAST_PERMUTATIONS) == []


def test_lower_threshold_sees_what_the_usual_one_misses():
    """
    Тот же движок с порогом в три дня. Так работает слой наблюдений: своей
    статистики у него нет, отличается только порог - и то, какими словами
    результат называют человеку.
    """
    timeline = Timeline()
    for offset in range(8):
        day = date(2026, 8, 1) + timedelta(days=offset)
        record = DayRecord(day=day)
        record.add(Fact("factor", "кофе", 1.0 if offset < 4 else 0.0))
        record.add(Fact("metric", "энергия", 8.0 if offset < 4 else 4.0))
        timeline.add(record)

    assert find_links(timeline, permutations=FAST_PERMUTATIONS) == []

    found = find_links(timeline, permutations=FAST_PERMUTATIONS,
                       min_days=OBSERVATION_MIN_DAYS)
    link = next(l for l in found if l.factor == "кофе" and l.metric == "энергия")
    assert link.days_with == 4 and link.days_without == 4
    # Средние по группам считает движок: слою наблюдений их не пересчитывать
    assert link.value_with == 8.0 and link.value_without == 4.0
    # Каким бы убедительным ни выглядел разрыв, на четырёх днях это наблюдение
    assert link.strength == "наблюдение"


def test_summary_counts_days():
    assert summarise(build(days=30))["дней в дневнике"] == 30
