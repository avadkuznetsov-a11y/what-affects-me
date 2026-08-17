"""Данные носимых устройств приводятся к общей шкале."""
from datetime import date

from wam.schema import DayRecord, Fact, Timeline
from wam.wearables import SberRingSource, merge_into, SLEEP, STRESS


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
