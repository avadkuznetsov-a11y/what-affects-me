"""
Синтетический дневник на 90 дней.

Нужен, чтобы демонстрацию можно было запустить без единой реальной записи и
без ключей к моделям. В данные зашиты две настоящие связи и один отвлекающий
фактор, который ни на что не влияет: движок обязан найти первые две и не
поднять шум вокруг третьего.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from wam.schema import DayRecord, Fact, Timeline

# Что зашито в данные:
#   кофе после 15:00  → сон хуже на следующий день (лаг 1)
#   тренировка        → энергия выше в тот же день (лаг 0)
#   сладкое           → ни на что не влияет (проверка на ложные срабатывания)
TRUE_EFFECT_COFFEE = -1.8
TRUE_EFFECT_WORKOUT = 1.4


def build(days: int = 90, seed: int = 42, start: date | None = None) -> Timeline:
    rng = random.Random(seed)
    start = start or date(2026, 5, 1)
    timeline = Timeline()

    coffee_by_day: dict[date, bool] = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        coffee_by_day[day] = rng.random() < 0.45

    for offset in range(days):
        day = start + timedelta(days=offset)
        record = DayRecord(day=day)

        coffee = coffee_by_day[day]
        workout = rng.random() < 0.4
        sweets = rng.random() < 0.5

        record.add(Fact("factor", "кофе", 1.0 if coffee else 0.0))
        record.add(Fact("factor", "тренировка", 1.0 if workout else 0.0))
        record.add(Fact("factor", "сладкое", 1.0 if sweets else 0.0))

        # Сон сегодня зависит от вчерашнего кофе
        yesterday_coffee = coffee_by_day.get(day - timedelta(days=1), False)
        sleep = 6.5 + (TRUE_EFFECT_COFFEE if yesterday_coffee else 0) + rng.gauss(0, 1.1)
        energy = 6.0 + (TRUE_EFFECT_WORKOUT if workout else 0) + rng.gauss(0, 1.2)

        record.add(Fact("metric", "качество сна", _clamp(sleep), "wearable"))
        record.add(Fact("metric", "энергия", _clamp(energy)))
        timeline.add(record)

    return timeline


def _clamp(value: float) -> float:
    return round(max(0.0, min(10.0, value)), 1)
