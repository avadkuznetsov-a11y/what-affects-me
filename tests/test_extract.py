"""Разбор рассказа: факты должны сводиться к общим именам."""
from datetime import date

from wam.extract import (LLMExtractor, RuleExtractor, day_mentioned,
                         measured_number)


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


def test_model_metrics_are_limited_to_known_names():
    """Модель не должна придумывать свои показатели - по ним не набрать статистики."""
    def fake(_prompt):
        return ('{"metrics":[{"name":"сон","value":4},{"name":"аура","value":9}],'
                '"factors":[{"name":"ссора с руководителем","value":1}]}')

    record = LLMExtractor(fake).extract("поругался, потом не мог уснуть", date(2026, 8, 1))
    assert record.metric("качество сна") == 4.0     # «сон» сведён к общему имени
    assert record.metric("аура") is None            # выдумка отброшена
    assert record.factor("ссора с руководителем") == 1.0


def test_measure_next_to_the_number_is_recognised():
    """«5 часов», «8 встреч», «две чашки» - это счёт чего-то, а не оценка по шкале."""
    assert measured_number("спал 5 часов")
    assert measured_number("сегодня 8 встреч")
    assert measured_number("выпил две чашки кофе")
    assert measured_number("пробежал 5 км")


def test_ordinary_word_after_the_number_is_not_a_measure():
    """
    Человек мнётся: «8 вроде», «на 6 где-то». По чёрному списку слов мерой
    становилось любое такое слово, и честная оценка пропадала молча.
    """
    assert not measured_number("8 вроде")
    assert not measured_number("на 7 кажется")
    assert not measured_number("на 6 где-то")
    assert not measured_number("9 точно")
    assert not measured_number("тревога 8 сегодня")
    assert not measured_number("8 из 10")


def test_measure_after_the_score_does_not_hide_it():
    """«на 8, бегал 5 км» - оценка названа первой, километры дальше про своё."""
    assert not measured_number("на 8, бегал 5 км")


def test_measure_before_the_number_counts_too():
    """
    «спал часов пять» - число стоит после меры, а не до неё. Пока смотрели
    только слово справа, такая фраза в ответ на вопрос со шкалой становилась
    оценкой: «тревога 5».
    """
    assert measured_number("спал часов пять")
    assert measured_number("часов шесть где-то")
    assert not measured_number("на 7")      # «на» мерой не было и не станет


def test_habits_are_counted_things_too():
    """«выпил 2 кофе», «2 пива вечером» - это счёт привычки, а не оценка."""
    assert measured_number("выпил 2 кофе")
    assert measured_number("2 пива вечером")
    assert measured_number("3 тренировки на неделе")


def test_model_zeros_for_unmentioned_metrics_are_dropped():
    """Модель любит дописать нули по всем показателям сразу - это не данные."""
    def fake(_prompt):
        return '{"metrics":[{"name":"тревога","value":0},{"name":"головная боль","value":0}]}'

    record = LLMExtractor(fake).extract("сходил на пробежку", date(2026, 8, 1))
    assert record.facts == []


# ── еда словами ───────────────────────────────────────────────────────────

def test_food_becomes_a_factor_like_everything_else():
    """«Что ел» - такой же фактор дня, как кофе или тренировка."""
    record = RuleExtractor().extract("ел много мяса и фастфуд", date(2026, 8, 1))
    assert record.factor("мясо") == 1.0
    assert record.factor("фастфуд") == 1.0


def test_food_words_collapse_to_common_names():
    for said, name in [("взял бургер на обед", "фастфуд"),
                       ("пицца вечером", "фастфуд"),
                       ("на ужин был лосось", "рыба"),
                       ("салат и брокколи", "овощи"),
                       ("булочка с кофе", "выпечка"),
                       ("творог утром", "молочное"),
                       ("наелся на ночь", "поздний ужин"),
                       ("объелся за ужином", "переедание"),
                       ("сегодня не обедал", "пропустил обед")]:
        record = RuleExtractor().extract(said, date(2026, 8, 1))
        assert record.factor(name) == 1.0, said


def test_sharp_pain_is_not_sharp_food():
    """«Острая боль» - это про голову, а не про еду; словарь их путать не должен."""
    record = RuleExtractor().extract("острая головная боль весь день", date(2026, 8, 1))
    assert record.factor("острое") is None
    assert RuleExtractor().extract("острая еда на обед", date(2026, 8, 1)).factor("острое") == 1.0


def test_food_can_be_absent_too():
    record = RuleExtractor().extract("сегодня не ел мяса совсем", date(2026, 8, 1))
    assert record.factor("мясо") == 0.0


# ── про какой день рассказывают ───────────────────────────────────────────

def test_yesterday_and_the_day_before():
    today = date(2026, 8, 20)               # четверг
    assert day_mentioned("вчера пил вино", today) == date(2026, 8, 19)
    assert day_mentioned("позавчера был перелёт", today) == date(2026, 8, 18)


def test_weekday_points_to_the_last_such_day():
    today = date(2026, 8, 20)               # четверг
    assert day_mentioned("в среду ходил в зал", today) == date(2026, 8, 19)
    assert day_mentioned("в понедельник был аврал", today) == date(2026, 8, 17)


def test_today_and_its_own_weekday_are_not_a_past_day():
    today = date(2026, 8, 20)               # четверг
    assert day_mentioned("сегодня пил кофе", today) is None
    assert day_mentioned("в четверг пил кофе", today) is None


def test_a_day_deep_inside_the_phrase_is_not_its_topic():
    """
    «на 7, но это скорее из-за того что вчера лёг рано» - оценка за сегодня.
    Увести такую реплику во вчерашний день значит потерять и оценку, и день.
    """
    today = date(2026, 8, 20)
    assert day_mentioned("на 7, но это скорее из-за того что вчера лёг рано", today) is None


RULES = RuleExtractor()


def test_time_of_day_found_through_the_whole_clause():
    """«Вечером выпил с друзьями пива» - вечер относится к пиву."""
    record = RULES.extract("вечером выпил с друзьями пива, наверное лишнее",
                           date.today())
    names = [f.name for f in record.facts if f.kind == "factor"]
    assert "алкоголь вечером" in names


def test_different_clauses_keep_their_own_time():
    record = RULES.extract("утром кофе, вечером тренировка в зале", date.today())
    names = [f.name for f in record.facts if f.kind == "factor"]
    assert "кофе утром" in names and "тренировка вечером" in names
    assert "кофе вечером" not in names


def test_no_strength_left_is_a_low_score():
    record = RULES.extract("сил вообще нет", date.today())
    assert record.metric("энергия") == 2.0


def test_guilt_is_not_wine():
    """«Виноват» и «не моя вина» - не алкоголь."""
    record = RULES.extract("откуда ты знаешь что кофе виноват", date.today())
    assert record.factor("алкоголь") is None


def test_heavy_event_is_one_factor():
    for said in ("умер дедушка", "весь день на похоронах", "меня уволили",
                 "весь день в больнице"):
        record = RULES.extract(said, date.today())
        assert record.factor("тяжёлое событие") == 1.0, said


def test_moderate_drinking_is_not_a_heavy_event():
    record = RULES.extract("пил умеренно, два бокала вина", date.today())
    assert record.factor("тяжёлое событие") is None
    assert record.factor("алкоголь") == 1.0


def test_day_can_be_found_anywhere_for_corrections():
    today = date(2026, 8, 21)                    # пятница
    assert day_mentioned("а нет, вру, это был вторник", today) is None
    assert day_mentioned("а нет, вру, это был вторник", today,
                         anywhere=True) == date(2026, 8, 18)
