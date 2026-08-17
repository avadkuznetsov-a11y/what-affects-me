"""
Источники объективных данных: носимые устройства, календарь, погода.

Продукт не измеряет физиологию сам — он берёт то, что уже измерено, и
добавляет к рассказу человека. Первый по приоритету источник для России —
Умное кольцо Sber: оно даёт сон, стресс, энергию, пульс и сатурацию, то есть
ровно те метрики, которые человек не может оценить сам, но которые чаще
всего оказываются следствием событий его жизни.

Формат внутри один для всех источников, поэтому добавление нового устройства
не затрагивает движок поиска связей.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .schema import DayRecord, Fact, Timeline

# Канонические имена метрик. Разные устройства называют одно и то же
# по-своему; сводим к общему словарю, иначе связи не наберут статистики.
SLEEP = "качество сна"
STRESS = "стресс"
ENERGY = "энергия"
STEPS = "активность"
HEART_RATE = "пульс покоя"
SPO2 = "сатурация"


@dataclass
class DailyReading:
    """Показатели за сутки от одного устройства."""

    day: date
    metrics: dict[str, float]
    source: str = "wearable"


class SberRingSource:
    """
    Умное кольцо Sber. Публичного пользовательского API у устройства нет,
    поэтому поддерживаем два пути: выгрузка из приложения (JSON/CSV) и —
    в случае партнёрства — прямой обмен. Слой один и тот же: на выходе
    DailyReading в канонических именах.
    """

    #  как поле называется у источника  →  каноническое имя
    MAPPING = {
        "sleep_score": SLEEP,
        "stress_level": STRESS,
        "energy": ENERGY,
        "steps": STEPS,
        "resting_hr": HEART_RATE,
        "spo2": SPO2,
    }

    name = "sber_ring"

    def read(self, payload: Iterable[dict]) -> list[DailyReading]:
        readings: list[DailyReading] = []
        for row in payload:
            day = _as_date(row.get("date"))
            if day is None:
                continue
            metrics = {
                canonical: float(row[key])
                for key, canonical in self.MAPPING.items()
                if row.get(key) is not None
            }
            if metrics:
                readings.append(DailyReading(day, _normalise(metrics), self.name))
        return readings


class AppleHealthSource(SberRingSource):
    """Тот же разбор, другие имена полей — на случай, если у человека iPhone."""

    MAPPING = {
        "sleepAnalysis": SLEEP,
        "stepCount": STEPS,
        "restingHeartRate": HEART_RATE,
        "oxygenSaturation": SPO2,
    }
    name = "apple_health"


def merge_into(timeline: Timeline, readings: Iterable[DailyReading]) -> Timeline:
    """Добавить показания устройств в дни временной линии, не затирая рассказ."""
    by_day = {record.day: record for record in timeline.days}
    for reading in readings:
        record = by_day.get(reading.day)
        if record is None:
            record = DayRecord(day=reading.day)
            timeline.add(record)
            by_day[reading.day] = record
        known = record.names("metric")
        for name, value in reading.metrics.items():
            if name in known:
                continue  # то, что человек сказал сам, важнее показаний прибора
            record.add(Fact("metric", name, value, reading.source))
    return timeline


def _normalise(metrics: dict[str, float]) -> dict[str, float]:
    """
    Всё приводим к шкале 0..10, где больше — лучше самочувствие.
    Стресс инвертируем: высокий стресс — это низкое значение метрики.
    """
    out: dict[str, float] = {}
    for name, value in metrics.items():
        if name == STRESS:
            out[name] = round(10 - _to_ten(value), 1)
        elif name == STEPS:
            out[name] = round(min(10.0, value / 1000), 1)
        else:
            out[name] = _to_ten(value)
    return out


def _to_ten(value: float) -> float:
    """Значения 0..100 сжимаем к 0..10, значения 0..10 оставляем как есть."""
    return round(value / 10, 1) if value > 10 else round(float(value), 1)


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
