"""Данные носимых устройств приводятся к общей шкале."""
from datetime import date, timedelta

from wam.derive import derive_factors
from wam.schema import DayRecord, Fact, Timeline
from wam.wearables import (NORM_MIN_DAYS, SberRingSource, merge_into, own_norm,
                           SLEEP, STRESS)


def test_ring_normalises_to_ten_point_scale():
    readings = SberRingSource().read([
        {"date": "2026-08-01", "sleep_score": 80, "stress_level": 70, "energy": 45},
    ])
    assert readings[0].metrics[SLEEP] == 8.0
    assert readings[0].metrics[STRESS] == 3.0   # высокий стресс = низкий балл


def test_diary_wins_over_device():
    timeline = Timeline()
    record = DayRecord(day=date(2026, 8, 1))
    record.add(Fact("metric", SLEEP, 2.0, "diary"))
    timeline.add(record)

    merge_into(timeline, SberRingSource().read([{"date": "2026-08-01", "sleep_score": 90}]))
    assert timeline.days[0].metric(SLEEP) == 2.0


def test_device_creates_missing_day():
    timeline = Timeline()
    merge_into(timeline, SberRingSource().read([{"date": "2026-08-02", "sleep_score": 60}]))
    assert len(timeline) == 1


# ── собственная норма человека ────────────────────────────────────────────

def test_own_norm_needs_a_week_of_days():
    """Три дня - это не «обычное», от такого среднего отсчитывать нельзя."""
    day = date(2026, 8, 30)
    series = {day - timedelta(days=i): 5.0 for i in range(1, NORM_MIN_DAYS)}
    assert own_norm(series, day) is None

    series[day - timedelta(days=NORM_MIN_DAYS)] = 5.0
    assert own_norm(series, day) == 5.0


def test_own_norm_counts_only_past_days_inside_the_window():
    """Сегодняшнее значение в свою же норму не идёт, слишком старое - тоже."""
    day = date(2026, 8, 30)
    series = {day - timedelta(days=i): 5.0 for i in range(1, NORM_MIN_DAYS + 1)}
    series[day] = 0.0
    series[day - timedelta(days=40)] = 10.0
    assert own_norm(series, day) == 5.0


def _ring_days(values: list[float], field: str = "hrv") -> Timeline:
    """Дневник из показаний кольца: по дню на значение, последнее - сегодня."""
    first = date.today() - timedelta(days=len(values) - 1)
    readings = SberRingSource().read([
        {"date": (first + timedelta(days=number)).isoformat(), field: value}
        for number, value in enumerate(values)])
    return merge_into(Timeline(), readings)


def test_low_hrv_that_is_normal_for_this_person_is_not_a_factor():
    """
    Тридцать миллисекунд - «низко» по общей мерке, но если у человека так
    каждый день, то сегодня он восстановился ровно как обычно.
    """
    timeline = derive_factors(_ring_days([30.0] * 10))
    assert timeline.days[-1].factor("организм не восстановился") == 0.0


def test_hrv_below_own_norm_is_a_factor_even_when_it_looks_high():
    """
    Шестьдесят миллисекунд по общей мерке - хорошо, а для человека с обычными
    восемьюдесятью это провал. Считаем от его нормы, а не от общей границы.
    """
    timeline = derive_factors(_ring_days([80.0] * 9 + [60.0]))
    assert timeline.days[-1].factor("организм не восстановился") == 1.0
    assert timeline.days[-1].factor("хорошее восстановление") == 0.0


def test_resting_pulse_is_counted_from_the_persons_own_norm():
    """Шестьдесят ударов - норма, но не для того, у кого обычно пятьдесят."""
    timeline = derive_factors(_ring_days([50.0] * 9 + [60.0], field="resting_hr"))
    assert timeline.days[-1].factor("высокий пульс") == 1.0

    timeline = derive_factors(_ring_days([60.0] * 10, field="resting_hr"))
    assert timeline.days[-1].factor("высокий пульс") == 0.0


def test_without_a_week_of_days_the_general_threshold_works():
    """
    Норма ещё не набралась - остаётся общая граница. Она грубая, зато есть с
    первого дня: три дня по 30 мс дают фактор, хотя для человека это может
    оказаться его обычным.
    """
    timeline = derive_factors(_ring_days([30.0, 30.0, 30.0]))
    assert timeline.days[-1].factor("организм не восстановился") == 1.0
