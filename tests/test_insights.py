"""Движок связей: находит настоящее и молчит про случайное."""
import random
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


def test_confounder_found_when_third_factor_is_strong():
    """
    Аврал заставляет пить кофе и сам портит сон. Программа обязана сказать,
    что дело не в кофе.

    Проверка внутри слоёв раньше требовала по четыре дня в каждой части - и
    при сильном третьем факторе отключалась молча: дней «с кофе, но без
    аврала» столько не набиралось. Именно этот случай и есть самый частый.
    """
    rng = random.Random(4)
    line = Timeline()
    start = date(2026, 3, 1)
    for offset in range(70):
        record = DayRecord(day=start + timedelta(days=offset))
        rush = rng.random() < 0.45
        record.add(Fact("factor", "аврал", 1.0 if rush else 0.0, "diary"))
        drank = rng.random() < (0.85 if rush else 0.2)
        record.add(Fact("factor", "кофе", 1.0 if drank else 0.0, "diary"))
        sleep = 7.0 + rng.gauss(0, 0.7) - (2.2 if rush else 0.0)
        record.add(Fact("metric", "качество сна", max(0.0, min(10.0, sleep)), "diary"))
        line.add(record)

    links = find_links(line, permutations=FAST_PERMUTATIONS)
    coffee = [l for l in links if l.factor == "кофе" and l.metric == "качество сна"]
    assert coffee, "связь «кофе - сон» в данных есть, её надо хотя бы увидеть"
    assert all(l.strength != "подтверждено" for l in coffee)
    assert any(l.confounder == "аврал" for l in coffee)


def test_choosing_the_best_lag_is_paid_for():
    """
    У каждой пары проверяются три задержки, и берётся самая выраженная. Это
    само по себе завышает эффект, поэтому поправка на множественные сравнения
    обязана считать все три, а не только пары.
    """
    rng = random.Random(9)
    line = Timeline()
    start = date(2026, 4, 1)
    for offset in range(50):
        record = DayRecord(day=start + timedelta(days=offset))
        record.add(Fact("factor", "кофе", 1.0 if rng.random() < 0.5 else 0.0, "diary"))
        record.add(Fact("metric", "энергия", round(rng.uniform(3, 8), 1), "diary"))
        line.add(record)

    links = find_links(line, permutations=FAST_PERMUTATIONS)
    for link in links:
        # Округление до тысячных делает своё дело, поэтому сравниваем с ним же
        assert link.p_adjusted >= round(min(1.0, link.p_value * 3), 4) - 1e-9


def test_usual_good_days_do_not_burn_every_day():
    """
    У человека, который стабильно спит на восьмёрку и ходит по восемь тысяч
    шагов, каждый день горело «хорошо выспался» и «много двигался». Фактор,
    который случается всегда, ничего не объясняет и в выводы не попадёт
    никогда - а в записи дня он мозолит глаза.
    """
    from wam.derive import derive_factors
    from wam.wearables import SberRingSource, merge_into

    rng = random.Random(3)
    line = Timeline()
    start = date(2026, 5, 1)
    rows = [{"date": (start + timedelta(days=o)).isoformat(),
             "sleep_score": 78 + rng.gauss(0, 2),
             "steps": 8200 + rng.gauss(0, 300)} for o in range(30)]
    merge_into(line, SberRingSource().read(rows))
    derive_factors(line)

    burning = [r for r in line.days[10:] if r.factor("хорошо выспался") == 1.0]
    assert len(burning) <= len(line.days[10:]) * 0.2


def test_small_effect_is_not_called_a_link():
    """
    Разницу в полбалла человек на себе не различает, а звучит она так же
    весомо, как разница в два балла - и потом не находится в его жизни.
    """
    rng = random.Random(11)
    line = Timeline()
    start = date(2026, 6, 1)
    for offset in range(80):
        record = DayRecord(day=start + timedelta(days=offset))
        drank = offset % 2 == 0
        record.add(Fact("factor", "кофе", 1.0 if drank else 0.0, "diary"))
        record.add(Fact("metric", "энергия",
                        6.0 - (0.5 if drank else 0.0) + rng.gauss(0, 0.3), "diary"))
        line.add(record)

    links = find_links(line, permutations=FAST_PERMUTATIONS)
    coffee = [l for l in links if l.factor == "кофе"]
    assert coffee and all(l.strength == "наблюдение" for l in coffee)


def test_permutation_p_is_never_a_flat_zero():
    """
    «Ни одна из двухсот перестановок» - это не «никогда». Пока ноль принимался
    как есть, дневник из чистого шума получал уверенный вывод.
    """
    from wam.insights import _permutation_p

    rng = random.Random(5)
    a = [8.0, 8.4, 7.6, 8.2, 7.8, 8.1, 7.9, 8.3, 8.0, 7.7]
    b = [3.0, 3.4, 2.6, 3.2, 2.8, 3.1, 2.9, 3.3, 3.0, 2.7]
    p_value = _permutation_p(a, b, 5.0, rng, permutations=200)
    assert 0 < p_value < 0.01
