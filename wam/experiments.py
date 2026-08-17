"""
Личный эксперимент — то, чем продукт отличается от трекеров.

Найденная связь остаётся догадкой, пока человек её не проверил. Поэтому из
каждой связи рождается короткий эксперимент: несколько дней с фактором,
несколько без, затем честный вердикт — подтвердилось, не подтвердилось или
данных не хватило. Никаких «вероятно, вам стоит меньше пить кофе».
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean

from .insights import Link, _permutation_p
from .schema import Timeline

MIN_DAYS_PER_ARM = 5


@dataclass
class Experiment:
    """План проверки одной гипотезы на конкретном человеке."""

    factor: str
    metric: str
    lag_days: int
    days: int = 14
    started: date | None = None
    hypothesis: str = ""
    plan: list[str] = field(default_factory=list)

    @classmethod
    def from_link(cls, link: Link, start: date, days: int = 14) -> "Experiment":
        half = days // 2
        return cls(
            factor=link.factor,
            metric=link.metric,
            lag_days=link.lag_days,
            days=days,
            started=start,
            hypothesis=(
                f"«{link.factor}» делает «{link.metric}» {link.direction} "
                f"примерно на {abs(link.effect):.1f} балла"
            ),
            plan=[
                f"Дни 1–{half}: живём как обычно, ничего не меняем.",
                f"Дни {half + 1}–{days}: убираем фактор «{link.factor}» полностью.",
                f"Каждый день отмечаем «{link.metric}» — на это уходит несколько секунд.",
            ],
        )

    def window(self) -> tuple[date, date]:
        if self.started is None:
            raise ValueError("эксперимент не начат")
        return self.started, self.started + timedelta(days=self.days - 1)


@dataclass
class Verdict:
    """Результат эксперимента человеческим языком."""

    status: str              # подтвердилось | не подтвердилось | недостаточно данных
    effect: float
    days_with: int
    days_without: int
    p_value: float
    text: str


def evaluate(experiment: Experiment, timeline: Timeline, seed: int = 0) -> Verdict:
    """Сравнить дни с фактором и без него внутри окна эксперимента."""
    rng = random.Random(seed)
    start, end = experiment.window()

    factor_series = timeline.series("factor", experiment.factor)
    metric_series = timeline.series("metric", experiment.metric)

    with_factor: list[float] = []
    without_factor: list[float] = []
    for day, value in factor_series.items():
        if not (start <= day <= end):
            continue
        outcome = metric_series.get(day + timedelta(days=experiment.lag_days))
        if outcome is None:
            continue
        (with_factor if value > 0 else without_factor).append(outcome)

    if len(with_factor) < MIN_DAYS_PER_ARM or len(without_factor) < MIN_DAYS_PER_ARM:
        return Verdict(
            "недостаточно данных", 0.0, len(with_factor), len(without_factor), 1.0,
            "Пока рано делать вывод: слишком мало дней в одной из частей. "
            "Продолжим наблюдение ещё несколько дней.",
        )

    effect = mean(with_factor) - mean(without_factor)
    p_value = _permutation_p(with_factor, without_factor, effect, rng)

    if p_value <= 0.05 and abs(effect) >= 0.5:
        direction = "хуже" if effect < 0 else "лучше"
        return Verdict(
            "подтвердилось", round(effect, 2), len(with_factor), len(without_factor),
            round(p_value, 4),
            f"На ваших данных подтвердилось: с фактором «{experiment.factor}» "
            f"показатель «{experiment.metric}» {direction} на {abs(effect):.1f} балла. "
            f"Сравнили {len(with_factor)} дней с фактором и {len(without_factor)} без него.",
        )

    return Verdict(
        "не подтвердилось", round(effect, 2), len(with_factor), len(without_factor),
        round(p_value, 4),
        f"Связь не подтвердилась: разница между днями с «{experiment.factor}» и без него "
        f"укладывается в обычные колебания. Это тоже результат — одной гипотезой меньше.",
    )
