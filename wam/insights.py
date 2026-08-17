"""
Поиск связей — сердце продукта.

Здесь нет языковой модели, и это принципиально: цифры считает обычная
статистика, модель отвечает только за язык. Иначе выводам нельзя доверять,
а доверие — единственная причина, по которой человек будет вести дневник
дольше недели.

Что делаем: для каждой пары «фактор → метрика» сравниваем дни, когда фактор
был, с днями, когда его не было, с учётом задержки (вчерашний кофе влияет на
сегодняшний сон). Величину эффекта проверяем перестановочным тестом: если
перемешать метки дней много раз, как часто случайность даст такой же разрыв?
Так отсеиваются совпадения, которых при трёх десятках наблюдений набирается
много.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean, pstdev

from .schema import Timeline

# Порог наблюдений: меньше — не показываем ничего, чтобы не выдавать шум
# за открытие. Семь дней с фактором и семь без — минимум, при котором
# перестановочный тест вообще способен что-то различить.
MIN_DAYS_PER_GROUP = 7
MAX_LAG_DAYS = 2
PERMUTATIONS = 2000


@dataclass
class Link:
    """Найденная связь между фактором и метрикой."""

    factor: str
    metric: str
    lag_days: int
    effect: float            # разница средних в баллах шкалы 0..10
    days_with: int
    days_without: int
    p_value: float           # доля случайных перестановок с не меньшим эффектом
    p_adjusted: float = 1.0  # то же с поправкой на число проверенных гипотез
    source: str = "рассказ"  # откуда пришёл фактор: рассказ или прибор
    confounder: str = ""     # третий фактор, который может объяснять связь

    @property
    def direction(self) -> str:
        return "хуже" if self.effect < 0 else "лучше"

    @property
    def strength(self) -> str:
        """
        Три уровня вместо процентов: человеку нужно понимать, во что верить,
        а не читать статистику.
        """
        if self.confounder:
            return "объясняется другим"
        if self.p_adjusted <= 0.01 and abs(self.effect) >= 1.0:
            return "подтверждено"
        if self.p_adjusted <= 0.05:
            return "вероятная связь"
        return "наблюдение"

    @property
    def causal_note(self) -> str:
        """
        Что можно сказать про причину. Событие вчера и состояние сегодня —
        это уже направление во времени. Одновременные события так толковать
        нельзя: неизвестно, что было первым.
        """
        if self.confounder:
            return f"скорее всего дело в другом: «{self.confounder}»"
        if self.lag_days >= 1:
            return "причина возможна: фактор был раньше состояния"
        return "совпало в один день, причину так не установить"

    def describe(self) -> str:
        when = {0: "в тот же день", 1: "на следующий день", 2: "через день"}[self.lag_days]
        return (
            f"«{self.factor}» → «{self.metric}» {when}: "
            f"{self.direction} на {abs(self.effect):.1f} балла "
            f"({self.days_with} дней с фактором против {self.days_without} без, "
            f"уровень: {self.strength})"
        )


def find_links(timeline: Timeline, seed: int = 0) -> list[Link]:
    """Все связи, прошедшие порог наблюдений, отсортированные по силе эффекта."""
    rng = random.Random(seed)
    links: list[Link] = []

    from .derive import DERIVED_FROM

    for factor in sorted(timeline.factor_names()):
        factor_series = timeline.series("factor", factor)
        for metric in sorted(timeline.metric_names()):
            metric_series = timeline.series("metric", metric)
            # «мало спал» против «качества сна» в тот же день — это один и тот
            # же показатель с двух сторон, такая связь ничего не объясняет
            same_thing = DERIVED_FROM.get(factor) == metric
            for lag in range(MAX_LAG_DAYS + 1):
                if same_thing and lag == 0:
                    continue
                link = _test_pair(factor, factor_series, metric, metric_series, lag, rng)
                if link is not None:
                    links.append(link)

    # Одна и та же пара может дать связь на нескольких лагах — оставляем
    # самую выраженную, иначе человек получит три формулировки об одном.
    best: dict[tuple[str, str], Link] = {}
    for link in links:
        key = (link.factor, link.metric)
        if key not in best or abs(link.effect) > abs(best[key].effect):
            best[key] = link

    # Поправка на множественные сравнения. Когда факторов десяток, а метрик
    # пяток, часть «связей» возникает просто потому, что гипотез много.
    # Без этой поправки продукт врёт уверенным тоном — и теряет доверие
    # на первом же случае, когда человек проверит вывод сам.
    tests = max(1, len(timeline.factor_names()) * len(timeline.metric_names()))
    for link in best.values():
        link.p_adjusted = round(min(1.0, link.p_value * tests), 4)
        link.source = _source_of(timeline, link.factor)
        link.confounder = find_confounder(timeline, link)

    return sorted(best.values(), key=lambda l: (l.p_adjusted, -abs(l.effect)))


def find_confounder(timeline: Timeline, link: Link) -> str:
    """
    Проверка на третий фактор.

    Классический случай: аврал на работе заставляет пить кофе и сам по себе
    поднимает тревогу. Кофе тут ни при чём, но связь «кофе → тревога» в данных
    видна. Способ отличить: разделить дни по третьему фактору и посмотреть,
    остался ли эффект внутри каждой части. Если эффект почти исчез — связь
    объясняется этим третьим фактором, и говорить о причине нельзя.
    """
    for other in sorted(timeline.factor_names()):
        if other == link.factor:
            continue
        effects = []
        for group_value in (1.0, 0.0):
            effect = _effect_within(timeline, link, other, group_value)
            if effect is None:
                effects = []
                break
            effects.append(effect)
        if not effects:
            continue
        # Внутри обеих частей эффект должен сохраниться хотя бы наполовину
        if max(abs(e) for e in effects) < abs(link.effect) * 0.5:
            return other
    return ""


def _effect_within(timeline: Timeline, link: Link, other: str, group_value: float) -> float | None:
    """Разница средних по фактору link.factor только среди дней, где other == group_value."""
    factor_series = timeline.series("factor", link.factor)
    other_series = timeline.series("factor", other)
    metric_series = timeline.series("metric", link.metric)

    with_factor: list[float] = []
    without_factor: list[float] = []
    for day, value in factor_series.items():
        if other_series.get(day) != group_value:
            continue
        outcome = metric_series.get(day + timedelta(days=link.lag_days))
        if outcome is None:
            continue
        (with_factor if value > 0 else without_factor).append(outcome)

    if len(with_factor) < 4 or len(without_factor) < 4:
        return None       # слишком мало дней, чтобы судить
    return mean(with_factor) - mean(without_factor)


def _source_of(timeline: Timeline, factor: str) -> str:
    from .derive import DEVICE_SOURCES

    for record in timeline.days:
        for fact in record.facts:
            if fact.kind == "factor" and fact.name == factor:
                return "прибор" if fact.source in DEVICE_SOURCES else "рассказ"
    return "рассказ"


def _test_pair(factor, factor_series, metric, metric_series, lag, rng) -> Link | None:
    with_factor: list[float] = []
    without_factor: list[float] = []

    for day, value in factor_series.items():
        target_day = day + timedelta(days=lag)
        outcome = metric_series.get(target_day)
        if outcome is None:
            continue
        (with_factor if value > 0 else without_factor).append(outcome)

    if len(with_factor) < MIN_DAYS_PER_GROUP or len(without_factor) < MIN_DAYS_PER_GROUP:
        return None

    effect = mean(with_factor) - mean(without_factor)
    if abs(effect) < 0.3:      # разница меньше трети балла человеку не нужна
        return None

    p_value = _permutation_p(with_factor, without_factor, effect, rng)
    return Link(factor, metric, lag, round(effect, 2),
                len(with_factor), len(without_factor), round(p_value, 4))


def _permutation_p(group_a: list[float], group_b: list[float], effect: float, rng) -> float:
    """Как часто случайное разделение тех же дней даёт не меньший разрыв."""
    pool = group_a + group_b
    size = len(group_a)
    observed = abs(effect)
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pool)
        if abs(mean(pool[:size]) - mean(pool[size:])) >= observed:
            hits += 1
    return hits / PERMUTATIONS


def summarise(timeline: Timeline) -> dict:
    """Короткая сводка для еженедельного разбора."""
    metrics = {}
    for metric in sorted(timeline.metric_names()):
        values = list(timeline.series("metric", metric).values())
        if len(values) >= 3:
            metrics[metric] = {
                "среднее": round(mean(values), 1),
                "разброс": round(pstdev(values), 1),
                "наблюдений": len(values),
            }
    return {"дней в дневнике": len(timeline), "метрики": metrics}
