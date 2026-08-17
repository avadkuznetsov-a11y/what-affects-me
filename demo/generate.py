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
#   кофе после 15:00  → сон хуже на следующий день (лаг 1) — настоящая связь
#   тренировка        → энергия выше в тот же день (лаг 0) — настоящая связь
#   сладкое           → ни на что не влияет — проверка на ложные срабатывания
#   аврал             → заставляет пить кофе И сам поднимает тревогу.
#                       Связь «кофе → тревога» в данных видна, но кофе тут ни
#                       при чём. Движок обязан назвать настоящую причину.
#   плохой сон        → на следующий день меньше энергии. Сон приходит с
#                       прибора, то есть причиной может быть и показание датчика.
TRUE_EFFECT_COFFEE = -1.8
TRUE_EFFECT_WORKOUT = 1.4
TRUE_EFFECT_RUSH = -2.2
TRUE_EFFECT_BAD_SLEEP = -1.5


def build(days: int = 90, seed: int = 42, start: date | None = None) -> Timeline:
    rng = random.Random(seed)
    start = start or date(2026, 5, 1)
    timeline = Timeline()

    rush_by_day: dict[date, bool] = {}
    coffee_by_day: dict[date, bool] = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        rush = rng.random() < 0.35
        rush_by_day[day] = rush
        # В аврал кофе пьют почти всегда, в спокойный день — редко
        coffee_by_day[day] = rng.random() < (0.85 if rush else 0.25)

    sleep_by_day: dict[date, float] = {}

    for offset in range(days):
        day = start + timedelta(days=offset)
        record = DayRecord(day=day)

        coffee = coffee_by_day[day]
        rush = rush_by_day[day]
        workout = rng.random() < 0.4
        sweets = rng.random() < 0.5

        record.add(Fact("factor", "кофе", 1.0 if coffee else 0.0))
        record.add(Fact("factor", "аврал", 1.0 if rush else 0.0))
        record.add(Fact("factor", "тренировка", 1.0 if workout else 0.0))
        record.add(Fact("factor", "сладкое", 1.0 if sweets else 0.0))

        # Сон сегодня зависит от вчерашнего кофе, приходит с кольца
        yesterday_coffee = coffee_by_day.get(day - timedelta(days=1), False)
        sleep = 6.5 + (TRUE_EFFECT_COFFEE if yesterday_coffee else 0) + rng.gauss(0, 1.1)
        sleep_by_day[day] = sleep

        # Энергия падает после плохого сна накануне — причина из показаний прибора
        yesterday_sleep = sleep_by_day.get(day - timedelta(days=1))
        slept_badly = yesterday_sleep is not None and yesterday_sleep < 5.0
        energy = (6.0 + (TRUE_EFFECT_WORKOUT if workout else 0)
                  + (TRUE_EFFECT_BAD_SLEEP if slept_badly else 0) + rng.gauss(0, 1.2))

        # Тревогу поднимает аврал, а не кофе — хотя в аврал кофе пьют чаще
        anxiety = 7.0 + (TRUE_EFFECT_RUSH if rush else 0) + rng.gauss(0, 1.0)

        record.add(Fact("metric", "качество сна", _clamp(sleep), "wearable"))
        record.add(Fact("metric", "энергия", _clamp(energy)))
        record.add(Fact("metric", "тревога", _clamp(anxiety)))
        timeline.add(record)

    return timeline


def _clamp(value: float) -> float:
    return round(max(0.0, min(10.0, value)), 1)
