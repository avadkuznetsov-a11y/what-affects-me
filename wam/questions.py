"""
Уточняющие вопросы.

Человек рассказывает про день как получится: где-то назовёт привычку и забудет
про самочувствие, где-то скажет «тревожно», но не скажет насколько. Спрашивать
всё подряд нельзя - это превратится в анкету, из-за которых люди и бросают
дневники. Поэтому вопрос всегда один и всегда тот, без которого сейчас не
получается вывод.

Порядок важности:
1. Не понял вообще ничего - подсказать, как сказать.
2. Есть привычки, но нет ни одного состояния - не с чем связывать.
3. Идёт проверка гипотезы - спросить именно про её показатель.
4. Названо состояние без оценки - уточнить силу.
"""
from __future__ import annotations

from .insights import Link
from .schema import DayRecord

# Как спросить про конкретный показатель
_ASK_METRIC = {
    "качество сна":  "Сколько часов удалось поспать?",
    "энергия":       "Насколько хватало сил сегодня, от 0 до 10?",
    "тревога":       "Насколько сильной была тревога, от 0 до 10?",
    "настроение":    "Как настроение по шкале от 0 до 10?",
    "головная боль": "Насколько сильно болела голова, от 0 до 10?",
}

NOTHING_UNDERSTOOD = (
    "Не понял, что записать. Скажите, что делали и как себя чувствовали, "
    "например: «пил кофе, спал часов пять, с утра тревожно»."
)

NO_STATE = (
    "Записал. А как вы себя чувствовали? Без этого не с чем связывать привычки - "
    "хватит пары слов: «выспался», «разбитый», «спокойный день»."
)


def next_question(record: DayRecord, links: list[Link] | None = None) -> str | None:
    """Один вопрос, который стоит задать после этой записи. None - вопросов нет."""
    if not record.facts:
        return NOTHING_UNDERSTOOD

    factors = {f.name for f in record.facts if f.kind == "factor" and f.value > 0}
    metrics = {f.name for f in record.facts if f.kind == "metric"}

    if factors and not metrics:
        return NO_STATE

    # Если по названной привычке уже проверяется гипотеза, спрашиваем ровно то,
    # чего не хватает для её проверки
    for link in links or []:
        if link.factor in factors and link.metric not in metrics:
            question = _ASK_METRIC.get(link.metric)
            if question:
                reason = f" Проверяем, как на это влияет «{link.factor}»."
                return question + reason

    # Состояние названо словом, но без силы: «тревога какая-то» - это оценка
    # по умолчанию, лучше уточнить у человека
    for fact in record.facts:
        if fact.kind == "metric" and fact.name in ("тревога", "головная боль") and fact.value == 3.0:
            return _ASK_METRIC[fact.name]

    return None


def apply_answer(record: DayRecord, question: str, answer: str) -> DayRecord:
    """
    Разобрать ответ на уточняющий вопрос и дописать его в тот же день.

    Ответ короткий и без контекста: «на 7», «часов шесть», «нормально».
    Поэтому сначала ищем в нём просто число - в ответе на вопрос со шкалой
    это самый частый случай, - и только потом пробуем общий разбор.
    """
    import re

    from .extract import RuleExtractor, Fact, _word_score

    metric = next((name for name, text in _ASK_METRIC.items() if text in question), "")
    if not metric:
        return record

    value = None
    lowered = answer.lower().strip()

    if metric == "качество сна":
        parsed = RuleExtractor().extract(f"спал {lowered}", record.day)
        value = parsed.metric("качество сна")

    if value is None:
        number = re.search(r"\b(10|\d)\b", lowered)
        if number:
            value = float(number.group(1))

    if value is None:
        value = _word_score(lowered)

    if value is None:
        return record

    record.facts = [f for f in record.facts if not (f.kind == "metric" and f.name == metric)]
    record.add(Fact("metric", metric, max(0.0, min(10.0, value)), "diary", quote=answer))
    return record
