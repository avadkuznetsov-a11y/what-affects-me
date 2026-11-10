"""Замер разбора речи: разметка цела, а сверка считает то, что обещает."""
import re
from datetime import date

from tools import measure_parsing as mp
from wam.schema import DayRecord, Fact


def test_every_pattern_in_markup_compiles():
    for text, items in mp.PHRASES:
        assert items, f"фраза без разметки: {text}"
        for kind, what, _ in items:
            if kind == "f":
                re.compile(what)
            else:
                assert set(what) <= {"качество сна", "энергия", "тревога",
                                     "настроение", "головная боль"}


def test_found_everything_means_whole_phrase():
    phrases = [("пил кофе, спал плохо", [mp.f(mp.COFFEE), mp.m(mp.SLEEP, "-")])]
    record = DayRecord(day=date.today())
    record.add(Fact("factor", "кофе", 1.0, "diary"))
    record.add(Fact("metric", "качество сна", 3.0, "diary"))
    result = mp.score([record], phrases)
    assert (result.found, result.items, result.whole) == (2, 2, 1)


def test_wrong_direction_and_denial_are_misses():
    phrases = [("кофе не пил, спал отлично", [mp.f(mp.COFFEE, 0.0), mp.m(mp.SLEEP, "+")])]
    record = DayRecord(day=date.today())
    record.add(Fact("factor", "кофе", 1.0, "diary"))          # отрицание потеряно
    record.add(Fact("metric", "качество сна", 3.0, "diary"))  # не в ту сторону
    result = mp.score([record], phrases)
    assert result.found == 0 and result.whole == 0


def test_model_silence_counts_as_empty_not_as_crash():
    result = mp.score([None], [("кофе", [mp.f(mp.COFFEE)])])
    assert result.errors == 1 and result.empty == 1 and result.found == 0


def test_timed_factor_is_not_extra():
    phrases = [("кофе вечером", [mp.f(mp.COFFEE)])]
    record = DayRecord(day=date.today())
    record.add(Fact("factor", "кофе", 1.0, "diary"))
    record.add(Fact("factor", "кофе вечером", 1.0, "diary"))
    assert mp.score([record], phrases).extra == []


def test_rules_run_covers_all_phrases():
    result = mp.score(mp.by_rules())
    assert result.phrases == len(mp.PHRASES)
    # Правила понимают не всё - ради этого замер и написан. Но если они вдруг
    # перестали понимать почти всё, сломан словарь, а не речь.
    assert 0.3 < result.found / result.items < 1.0
