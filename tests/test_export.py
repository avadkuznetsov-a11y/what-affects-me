"""
Выгрузка дневника: то, что человек несёт врачу.

Проверяем не красоту файла, а то, что из него ничего не потерялось и не
появилось лишнего: все дни на месте, оценки в той шкале, в какой человек их
называл, догадок программы в файле нет.
"""
import csv
import io
from datetime import date, timedelta

from wam.export import as_csv, as_text
from wam.habits import SOURCE as IMPLIED
from wam.schema import DayRecord, Fact, Timeline

TODAY = date(2026, 8, 20)


def _month() -> Timeline:
    """Месяц записей: слова человека, привычки и показания кольца."""
    timeline = Timeline()
    for ago in range(30):
        record = DayRecord(day=TODAY - timedelta(days=ago))
        record.add(Fact("metric", "энергия", 4.0 + ago % 3, "diary"))
        record.add(Fact("metric", "тревога", 3.0, "diary"))
        record.add(Fact("metric", "качество сна", 6.5, "sber_ring"))
        record.add(Fact("factor", "кофе", 1.0, "diary"))
        timeline.add(record)
    return timeline


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))


# ── ничего не потерялось ──────────────────────────────────────────────────

def test_every_day_is_in_the_text():
    text = as_text(_month(), made_on=TODAY)
    for ago in range(30):
        assert (TODAY - timedelta(days=ago)).strftime("%d.%m.%Y") in text
    assert "дней с записями: 30" in text


def test_every_day_is_a_row_in_the_table():
    rows = _rows(as_csv(_month()))
    assert len(rows) == 31                      # заголовок и тридцать дней
    assert rows[1][0] == (TODAY - timedelta(days=29)).strftime("%d.%m.%Y")
    assert rows[-1][0] == TODAY.strftime("%d.%m.%Y")


def test_days_go_in_order():
    """Врач читает историю сверху вниз: когда началось - важнее, чем как сейчас."""
    rows = _rows(as_csv(_month()))
    dates = [row[0] for row in rows[1:]]
    assert dates == sorted(dates, key=lambda text: text.split(".")[::-1])


def test_the_table_opens_in_russian_excel():
    """
    Без BOM русский Excel читает UTF-8 как кракозябры, а с запятой-разделителем
    ломает колонки: файл, который нельзя открыть, никому не покажешь.
    """
    table = as_csv(_month())
    assert table.startswith("\ufeff")     # тот самый BOM
    assert table.splitlines()[0].count(";") >= 5


# ── цифры в том виде, в каком о них говорят ───────────────────────────────

def test_anxiety_is_written_the_way_the_person_says_it():
    """Внутри тройка означает сильную тревогу; человек называет её семёркой."""
    text = as_text(_month(), made_on=TODAY)
    assert "тревога 7" in text
    assert "тревога (сила)" in as_csv(_month()).splitlines()[0]


def test_the_scale_is_explained():
    text = as_text(_month(), made_on=TODAY)
    assert "от 0 до 10" in text
    assert "чем больше, тем сильнее" in text


def test_device_readings_are_told_apart_from_the_story():
    """Что человек сказал, а что измерил прибор - в файле должно быть видно."""
    text = as_text(_month(), made_on=TODAY)
    assert "самочувствие: энергия" in text
    assert "с прибора: качество сна 6,5" in text
    assert "качество сна" in _rows(as_csv(_month()))[1][-1]


# ── чего в файле быть не должно ───────────────────────────────────────────

def test_guesses_of_the_program_do_not_go_into_the_file():
    """
    Дни без привычки программа достраивает сама (`wam/habits.py`). Это догадка,
    а не слова человека, и выдавать её за запись в дневнике нельзя.
    """
    timeline = Timeline()
    record = DayRecord(day=TODAY)
    record.add(Fact("factor", "кофе", 1.0, "diary"))
    record.add(Fact("factor", "алкоголь", 0.0, IMPLIED))
    timeline.add(record)

    text = as_text(timeline, made_on=TODAY)
    assert "было: кофе" in text
    assert "алкоголь" not in text


def test_device_factors_do_not_double_the_numbers():
    """«мало спал» рядом с «качество сна 3» читается как второе наблюдение."""
    timeline = Timeline()
    record = DayRecord(day=TODAY)
    record.add(Fact("metric", "качество сна", 3.0, "sber_ring"))
    record.add(Fact("factor", "мало спал", 1.0, "wearable"))
    timeline.add(record)

    assert "мало спал" not in as_text(timeline, made_on=TODAY)


def test_empty_diary_says_so():
    """Пустой файл человек прочтёт как поломку, а это просто пустой дневник."""
    text = as_text(Timeline(), made_on=TODAY)
    assert "Записей пока нет" in text
    assert len(_rows(as_csv(Timeline()))) == 1      # один заголовок


def test_own_words_are_kept():
    timeline = Timeline()
    timeline.add(DayRecord(day=TODAY, raw_text="весь день никакой, вечером кофе"))
    assert "своими словами: весь день никакой" in as_text(timeline, made_on=TODAY)
