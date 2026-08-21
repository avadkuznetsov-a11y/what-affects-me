"""
Модель данных: из чего складывается временная линия жизни человека.

Одна запись дневника — это не текст, а набор фактов на конкретный день:
что произошло (события), что человек делал (факторы) и как он себя
чувствовал (метрики состояния). Данные носимых устройств попадают сюда же,
поэтому связь можно искать между сном и разговором с начальником, а не
только внутри одного приложения.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

# Факторы — то, что может влиять: «кофе после 15:00», «тренировка», «перелёт».
# Метрики — то, на что влияет: «сон», «тревога», «энергия», «головная боль».
FactKind = Literal["factor", "metric", "event"]


@dataclass(frozen=True)
class Fact:
    """Единичное утверждение о дне: факт, вытащенный из рассказа или из данных."""

    kind: FactKind
    name: str
    value: float = 1.0          # для factor — 1/0 или количество, для metric — оценка
    source: str = "diary"       # diary | wearable | calendar | weather
    quote: str = ""             # фрагмент рассказа, из которого факт извлечён

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("у факта должно быть имя")


@dataclass
class DayRecord:
    """Всё, что известно про один день."""

    day: date
    facts: list[Fact] = field(default_factory=list)
    raw_text: str = ""

    def add(self, fact: Fact) -> None:
        """
        Добавить факт за день. Один показатель за день - одно значение:
        если сон уже записан, второй записи не будет. Сказанное человеком
        важнее показаний прибора, поэтому рассказ вытесняет кольцо, но не
        наоборот - человек знает про свою ночь больше, чем датчик.
        """
        existing = next((f for f in self.facts
                         if f.kind == fact.kind and f.name == fact.name), None)
        if existing is not None:
            from_device = existing.source not in ("diary", "")
            told_now = fact.source in ("diary", "")
            if from_device and told_now:
                self.facts.remove(existing)     # рассказ вытесняет прибор
            elif told_now and not from_device and fact.value != existing.value:
                # Человек сказал про то же другое: «сил ноль» после «сил на 3».
                # Свежее слово вытесняет прежнее - он уточняет свой день, а не
                # спорит сам с собой. Пока этого не было, поправка пропадала:
                # в дневнике оставалась первая цифра.
                self.facts.remove(existing)
            else:
                return                          # иначе оставляем то, что было
        self.facts.append(fact)

    def factor(self, name: str) -> float | None:
        """Значение фактора за день; None — если про него в этот день ничего не известно."""
        for f in self.facts:
            if f.kind == "factor" and f.name == name:
                return f.value
        return None

    def metric(self, name: str) -> float | None:
        for f in self.facts:
            if f.kind == "metric" and f.name == name:
                return f.value
        return None

    def names(self, kind: FactKind) -> set[str]:
        return {f.name for f in self.facts if f.kind == kind}


@dataclass
class Timeline:
    """Временная линия: дни по порядку. Основной вход для поиска связей."""

    days: list[DayRecord] = field(default_factory=list)

    def add(self, record: DayRecord) -> None:
        self.days.append(record)
        self.days.sort(key=lambda d: d.day)

    def factor_names(self) -> set[str]:
        return set().union(*(d.names("factor") for d in self.days)) if self.days else set()

    def metric_names(self) -> set[str]:
        return set().union(*(d.names("metric") for d in self.days)) if self.days else set()

    def series(self, kind: FactKind, name: str) -> dict[date, float]:
        """Значения одного показателя по дням — пропущенные дни отсутствуют в словаре."""
        out: dict[date, float] = {}
        for record in self.days:
            value = record.factor(name) if kind == "factor" else record.metric(name)
            if value is not None:
                out[record.day] = value
        return out

    def __len__(self) -> int:
        return len(self.days)
