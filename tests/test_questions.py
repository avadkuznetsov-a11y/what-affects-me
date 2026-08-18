"""Если чего-то не хватает, программа спрашивает - но только об одном."""
from datetime import date

from demo.generate import build
from wam.derive import derive_factors
from wam.extract import RuleExtractor
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
    links = find_links(derive_factors(build(days=120)))
    record = RuleExtractor().extract("Пил кофе, весь день бодрый", date(2026, 8, 1))
    question = next_question(record, links)
    assert "спалось" in question or "поспать" in question
    assert "кофе" in question


def test_does_not_repeat_the_same_question():
    """Один и тот же вопрос дважды - верный способ, чтобы человек бросил дневник."""
    links = find_links(derive_factors(build(days=120)))
    record = RuleExtractor().extract("Пил кофе, весь день бодрый", date(2026, 8, 1))
    first = next_question(record, links)
    assert next_question(record, links, asked={first}) != first


def test_silent_when_everything_is_clear():
    record = RuleExtractor().extract("Пил кофе, спал восемь часов, бодрый", date(2026, 8, 1))
    assert next_question(record) is None


def test_answer_updates_the_day():
    record = RuleExtractor().extract("Тревога какая-то", date(2026, 8, 1))
    assert record.metric("тревога") == 3.0          # оценка по умолчанию
    apply_answer(record, "Насколько сильной была тревога, от 0 до 10?", "на 7 баллов")
    assert record.metric("тревога") == 7.0          # заменилась ответом человека
