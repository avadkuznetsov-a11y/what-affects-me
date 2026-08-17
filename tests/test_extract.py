"""Разбор рассказа: факты должны сводиться к общим именам."""
from datetime import date

from wam.extract import RuleExtractor, LLMExtractor


def test_finds_factor_and_metric():
    record = RuleExtractor().extract("Пил кофе вечером, спал 5 часов", date(2026, 8, 1))
    assert record.factor("кофе") == 1.0
    assert record.metric("качество сна") is not None


def test_negation_means_absence():
    record = RuleExtractor().extract("Сегодня не пил кофе совсем", date(2026, 8, 1))
    assert record.factor("кофе") == 0.0


def test_synonyms_collapse_to_one_name():
    a = RuleExtractor().extract("Взял капучино утром", date(2026, 8, 1))
    b = RuleExtractor().extract("Выпил эспрессо", date(2026, 8, 2))
    assert a.factor("кофе") == b.factor("кофе") == 1.0


def test_word_score_without_digits():
    record = RuleExtractor().extract("Настроение ужасное", date(2026, 8, 1))
    assert record.metric("настроение") == 2.0


def test_llm_extractor_parses_wrapped_json():
    def fake(_prompt):
        return 'Вот разбор: {"factors":[{"name":"перелёт","value":1}],"metrics":[{"name":"энергия","value":3}]}'

    record = LLMExtractor(fake).extract("Летел утром, разбит", date(2026, 8, 1))
    assert record.factor("перелёт") == 1.0
    assert record.metric("энергия") == 3.0


def test_llm_extractor_survives_garbage():
    record = LLMExtractor(lambda _: "модель ответила ерундой").extract("текст", date(2026, 8, 1))
    assert record.facts == []


def test_hours_spelled_with_words():
    """«спал часов пять» — так говорят чаще, чем «спал 5 часов»."""
    record = RuleExtractor().extract("Спал часов пять, разбитый", date(2026, 8, 1))
    assert record.metric("качество сна") == 6.2


def test_bad_state_without_score():
    """Назвал тревогу и не поставил оценку — это точно не хороший день."""
    record = RuleExtractor().extract("Тревога какая-то весь день", date(2026, 8, 1))
    assert record.metric("тревога") == 3.0
