"""Выводы должны читаться как обычная речь, а не как отчёт статистика."""
from demo.generate import build
from wam.derive import derive_factors
from conftest import FAST_PERMUTATIONS
from wam.insights import find_links
from wam.phrases import say, basis, next_step, _days, _score


def _link(factor, metric):
    links = find_links(derive_factors(build(days=120)), permutations=FAST_PERMUTATIONS)
    return next(l for l in links if l.factor == factor and l.metric == metric)


def test_real_link_reads_like_speech():
    text = say(_link("кофе", "качество сна"))
    assert text.startswith("После кофе")
    assert "спится хуже" in text
    assert "балл" in text


def test_device_link_says_where_data_came_from():
    assert "по данным кольца" in basis(_link("мало спал", "энергия"))


def test_false_link_names_the_real_reason():
    link = _link("кофе", "тревога")
    assert "не в этом" in say(link)
    assert "аврал" in say(link)
    assert "аврал" in next_step(link)


def test_no_technical_words_in_user_text():
    """Человеку не показываем ни лаги, ни уровни значимости."""
    for link in find_links(derive_factors(build(days=120)), permutations=FAST_PERMUTATIONS):
        text = say(link) + basis(link) + next_step(link)
        for word in ("лаг", "p-value", "фактор →", "метрика"):
            assert word not in text


def test_russian_number_forms():
    assert _days(1) == "1 день" and _days(2) == "2 дня" and _days(11) == "11 дней"
    assert _score(2.0) == "2 балла" and "," in _score(1.6)
