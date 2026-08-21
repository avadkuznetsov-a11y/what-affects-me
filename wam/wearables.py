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
from datetime import date, timedelta
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

# Кольцо Sber меряет пульс круглосуточно, а вариабельность сердечного ритма и
# сатурацию - ночью; поверх этого его модели считают фазы сна, качество сна,
# уровень стресса и «ресурс» - запас сил на день. Ресурс мы кладём в «энергию»:
# это то же самое другими словами, а два имени для одного развалили бы
# статистику. Остальное добавляем как отдельные показатели.
HRV = "вариабельность пульса"     # выше - организм отдохнул, ниже - под нагрузкой
DEEP_SLEEP = "глубокий сон"       # доля глубокой фазы за ночь
REM_SLEEP = "быстрый сон"         # фаза сновидений, по ней видно восстановление
SLEEP_HOURS = "часов сна"         # сколько человек проспал по часам
WAKEUPS = "пробуждения за ночь"   # чем больше, тем рванее ночь
TEMPERATURE = "температура тела"  # отклонение от собственной нормы человека


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
    #
    # Имён на один показатель бывает несколько: выгрузка из приложения и обмен
    # по договорённости называют поля по-разному, а человек ещё и правит файл
    # руками. Лишние синонимы тут дешевле потерянных данных.
    MAPPING = {
        "sleep_score": SLEEP,
        "stress_level": STRESS,
        "energy": ENERGY,
        "resource": ENERGY,             # «ресурс» кольца - это и есть запас сил
        "steps": STEPS,
        "resting_hr": HEART_RATE,
        "spo2": SPO2,
        "hrv": HRV,
        "hrv_ms": HRV,
        "deep_sleep": DEEP_SLEEP,
        "rem_sleep": REM_SLEEP,
        "sleep_hours": SLEEP_HOURS,
        "sleep_duration": SLEEP_HOURS,
        "wakeups": WAKEUPS,
        "awakenings": WAKEUPS,
        "temperature": TEMPERATURE,
        "skin_temp": TEMPERATURE,
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


# ── собственная норма человека ────────────────────────────────────────────
#
# У вариабельности пульса, пульса покоя и температуры общей для всех границы
# нет: 30 мс вариабельности у одного человека - обычный день, а у другого
# тревожный сигнал. Так это считают и в мире: «готовность» Oura сравнивает
# сегодняшнюю вариабельность со скользящим средним самого человека за 28 дней,
# туда же идут тренд пульса покоя и отклонение температуры.
#
# Окно в 28 дней - это месяц его жизни: в нём уже есть и рабочие недели, и
# выходные, и простуда, но ещё нет смены сезона.
NORM_WINDOW_DAYS = 28

# Меньше недели - это не норма, а случайный набор дней: одна рваная ночь в
# среднем по трём дням сдвигает «обычное» так, что от него нельзя отсчитывать.
NORM_MIN_DAYS = 7


def own_norm(series: dict[date, float], day: date) -> float | None:
    """
    Собственная норма человека по показателю на этот день: среднее по его
    прошлым дням внутри окна. None - дней ещё мало, отсчитывать не от чего.

    Считаем по дням ДО названного: сегодняшнее значение в свою же норму
    попадать не должно, иначе отклонение смазывается о само себя.

    На вход идёт готовый ряд (`Timeline.series`), а не дневник целиком: норму
    спрашивают на каждый день и на каждое правило, и перебирать ради неё все
    факты заново - лишняя работа.
    """
    first = day - timedelta(days=NORM_WINDOW_DAYS)
    past = [value for when, value in series.items() if first <= when < day]
    if len(past) < NORM_MIN_DAYS:
        return None
    return sum(past) / len(past)


def _normalise(metrics: dict[str, float]) -> dict[str, float]:
    """
    Всё приводим к шкале 0..10, где больше — лучше самочувствие.

    Единицы у показателей разные: проценты, удары в минуту, миллисекунды, часы,
    градусы. Делить всё подряд на десять нельзя - так пульс 90 превращался в
    «9 из 10», то есть в отличное самочувствие. Поэтому у каждого показателя
    свой перевод, и у каждого сказано, откуда взяты границы.
    """
    out: dict[str, float] = {}
    for name, value in metrics.items():
        if name == STRESS:
            # Высокий стресс - это плохо, а шкала у нас «больше значит лучше»
            out[name] = round(10 - _to_ten(value), 1)
        elif name == STEPS:
            # Десять тысяч шагов - привычный ориентир «полного» дня
            out[name] = round(min(10.0, value / 1000), 1)
        elif name == HEART_RATE:
            # Пульс покоя: 50 и ниже - отдохнувшее сердце, 100 и выше - плохо.
            # Границы обиходные, из диапазона нормы для взрослого.
            out[name] = _between(value, best=50, worst=100)
        elif name == HRV:
            # Вариабельность в миллисекундах: чем выше, тем лучше восстановление.
            # Шкала логарифмическая, и это не украшение: разница между 14 и 26
            # мс для организма больше, чем между 86 и 98, а на прямой линейке
            # они одинаковы. При линейной шкале падение вариабельности на треть
            # у человека с низкой нормой не набирало даже балла - то есть самый
            # важный сигнал прибора терялся. В мире по этой же причине считают
            # логарифм RMSSD.
            out[name] = _log_between(value, best=120, worst=10)
        elif name == SLEEP_HOURS:
            # Восемь часов - десятка, четыре - пятёрка, как в разборе речи
            out[name] = round(min(10.0, value / 0.8), 1)
        elif name == WAKEUPS:
            # Ноль пробуждений - десятка, пять и больше - ноль
            out[name] = _between(value, best=0, worst=5)
        elif name == TEMPERATURE:
            # Приходит отклонением от собственной нормы человека. Важна любая
            # сторона: и жар, и переохлаждение - это «не как обычно».
            out[name] = _between(abs(value), best=0.0, worst=1.5)
        else:
            # Сон, энергия, сатурация, фазы сна - всё в процентах или баллах
            out[name] = _to_ten(value)
    return out


def _between(value: float, best: float, worst: float) -> float:
    """
    Перевести показатель в шкалу 0..10 по двум ориентирам: где хорошо и где
    плохо. Работает в обе стороны - у пульса «лучше» это меньше, у
    вариабельности больше.
    """
    if best == worst:
        return 5.0
    share = (value - worst) / (best - worst)
    return round(max(0.0, min(1.0, share)) * 10, 1)


def _log_between(value: float, best: float, worst: float) -> float:
    """
    То же, что `_between`, но по логарифму: у показателя, который меняется в
    разы, а не на проценты, равные шаги на прямой линейке неравны для тела.
    """
    from math import log

    value = max(value, 0.1)
    share = (log(value) - log(worst)) / (log(best) - log(worst))
    return round(max(0.0, min(1.0, share)) * 10, 1)


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
