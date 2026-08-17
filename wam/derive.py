"""
Превращение показаний датчиков в причины, а не только в следствия.

Кольцо меряет сон, стресс, активность и пульс. Обычно эти цифры считают
результатом: «сегодня плохо спал». Но вчерашний плохой сон — это уже причина
сегодняшней тревоги, а вчерашние две тысячи шагов — причина вечерней вялости.

Поэтому из показаний делаем такие же привычки-факторы, как «кофе» или
«тренировка», и дальше движок ищет связи одинаково, независимо от того,
пришёл факт из речи или с прибора.
"""
from __future__ import annotations

from .schema import DayRecord, Fact, Timeline

# Порог, ниже (или выше) которого показание превращается в факт.
# Шкала общая — 0..10, где больше значит лучше.
RULES: list[tuple[str, str, str, float]] = [
    # метрика датчика,   имя фактора,          направление, порог
    ("качество сна",     "мало спал",          "ниже",      5.0),
    ("качество сна",     "хорошо выспался",    "выше",      7.5),
    ("активность",       "мало двигался",      "ниже",      3.0),
    ("активность",       "много двигался",     "выше",      7.0),
    ("стресс",           "высокий стресс",     "ниже",      4.0),
    ("пульс покоя",      "высокий пульс",      "ниже",      4.0),
]

DEVICE_SOURCES = {"wearable", "sber_ring", "apple_health"}

# Какой фактор из какой метрики получен. Нужно, чтобы не сравнивать
# показание прибора с ним же: «мало спал» и «качество сна» — одно и то же,
# и связь между ними в тот же день ничего не объясняет.
DERIVED_FROM: dict[str, str] = {factor: metric for metric, factor, _, _ in RULES}


def derive_factors(timeline: Timeline) -> Timeline:
    """Добавить факторы, посчитанные из показаний приборов."""
    for record in timeline.days:
        for metric_name, factor_name, direction, threshold in RULES:
            if not _from_device(record, metric_name):
                continue
            value = record.metric(metric_name)
            if value is None:
                continue
            hit = value < threshold if direction == "ниже" else value > threshold
            record.add(Fact("factor", factor_name, 1.0 if hit else 0.0, "wearable"))
    return timeline


def _from_device(record: DayRecord, metric_name: str) -> bool:
    """Показание пришло с прибора, а не со слов человека."""
    for fact in record.facts:
        if fact.kind == "metric" and fact.name == metric_name:
            return fact.source in DEVICE_SOURCES
    return False


def factor_source(timeline: Timeline, factor_name: str) -> str:
    """Откуда взялся фактор: из рассказа или с прибора. Нужно для объяснений."""
    for record in timeline.days:
        for fact in record.facts:
            if fact.kind == "factor" and fact.name == factor_name:
                return "прибор" if fact.source in DEVICE_SOURCES else "рассказ"
    return "рассказ"
