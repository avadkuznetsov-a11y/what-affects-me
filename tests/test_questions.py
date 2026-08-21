"""Если чего-то не хватает, программа спрашивает - но только об одном."""
from datetime import date

from demo.generate import build
from wam.derive import derive_factors
from wam.extract import RuleExtractor
from conftest import FAST_PERMUTATIONS
from wam.insights import find_links
from wam.questions import next_question, apply_answer, NOTHING_UNDERSTOOD, NO_STATE
from wam.schema import DayRecord


def test_asks_when_nothing_understood():
    assert next_question(DayRecord(day=date(2026, 8, 1))) == NOTHING_UNDERSTOOD


def test_asks_about_state_when_only_habits_named():
    record = RuleExtractor().extract("Пил кофе вечером", date(2026, 8, 1))
    assert next_question(record) == NO_STATE


def test_asks_about_metric_under_test():
    """По кофе проверяется сон - значит и спрашиваем про сон, а не про всё подряд."""
    links = find_links(derive_factors(build(days=120)), permutations=FAST_PERMUTATIONS)
    record = RuleExtractor().extract("Пил кофе, весь день бодрый", date(2026, 8, 1))
    question = next_question(record, links)
    assert "спалось" in question or "поспать" in question
    assert "кофе" in question


def test_does_not_repeat_the_same_question():
    """Один и тот же вопрос дважды - верный способ, чтобы человек бросил дневник."""
    links = find_links(derive_factors(build(days=120)), permutations=FAST_PERMUTATIONS)
    record = RuleExtractor().extract("Пил кофе, весь день бодрый", date(2026, 8, 1))
    first = next_question(record, links)
    assert next_question(record, links, asked={first}) != first


def test_asks_about_the_habit_when_the_state_is_clear():
    """
    Самочувствие названо, но про привычку известно только то, что она была.
    Сколько чашек кофе и когда - без этого связь выйдет грубой, поэтому
    уточняем. Заказчик сказал прямо: «вопросы нужны всегда, если цель
    разобраться».
    """
    record = RuleExtractor().extract("Пил кофе, спал восемь часов, бодрый", date(2026, 8, 1))
    question = next_question(record)
    assert question is not None and "кофе" in question.lower()

    # Второй раз про то же не спрашиваем
    assert next_question(record, asked={question}) is None


def test_answer_updates_the_day():
    record = RuleExtractor().extract("Тревога какая-то", date(2026, 8, 1))
    assert record.metric("тревога") == 3.0          # оценка по умолчанию
    apply_answer(record, "Насколько сильной была тревога, от 0 до 10?", "на 7 баллов")
    # Спрашиваем «насколько СИЛЬНОЙ», а храним по шкале «больше значит лучше»:
    # сильная тревога на 7 - это плохой день, то есть 3 внутри. Без переворота
    # ответ записывался как спокойный день и все выводы по тревоге шли наизнанку.
    assert record.metric("тревога") == 3.0


def test_answer_spelled_with_a_word():
    """«ноль» на «от 0 до 10» - такой же ответ, только цифры в нём нет."""
    record = RuleExtractor().extract("Тревога какая-то", date(2026, 8, 1))
    apply_answer(record, "Насколько сильной была тревога, от 0 до 10?", "ноль")
    assert record.metric("тревога") == 10.0     # тревоги нет - день спокойный

    record = RuleExtractor().extract("Тревога какая-то", date(2026, 8, 1))
    apply_answer(record, "Насколько сильной была тревога, от 0 до 10?", "восемь")
    assert record.metric("тревога") == 2.0      # тревога на 8 - день тяжёлый


def test_does_not_ask_about_the_default_score_twice():
    """
    Оценка по умолчанию - 3.0, и ответ «3» её не меняет. Если спрашивать по
    значению, вопрос про силу тревоги пойдёт по кругу.
    """
    record = RuleExtractor().extract("Тревога какая-то", date(2026, 8, 1))
    question = next_question(record)
    assert question and next_question(record, asked={question}) is None


def test_answer_to_a_side_question_is_recorded_too():
    """Уточнение сверх сказанного спрашивается другими словами - ответ на него терялся."""
    record = RuleExtractor().extract("Настроение хорошее", date(2026, 8, 1))
    question = ("Кстати, сил сегодня хватало? Оцените от 0 до 10 - "
                "проверяю, влияет ли на это «кофе после 15:00».")
    apply_answer(record, question, "на 7")
    assert record.metric("энергия") == 7.0
