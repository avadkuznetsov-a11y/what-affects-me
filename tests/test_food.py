"""Еда числами: разбор выгрузки КБЖУ и факторы дня из порогов."""
from datetime import date

from wam import food
from wam.schema import DayRecord, Timeline

DAY = date(2026, 8, 1)


def _names(values):
    """Факторы, которые сработали, - те, у которых значение больше нуля."""
    return {f.name for f in food.day_factors(values) if f.value > 0}


def test_thresholds_come_from_the_daily_norm():
    """Пороги считаются от нормы, а не назначены числом: проверяем оба края."""
    norm = food.CALORIES_NORM
    assert "переел" in _names({"калории": norm * food.OVEREAT + 1})
    assert "переел" not in _names({"калории": norm})
    assert "недоел" in _names({"калории": norm * food.UNDEREAT - 1})
    assert "недоел" not in _names({"калории": norm})


def test_low_protein_and_carb_skew():
    assert "мало белка" in _names({"белки": food.PROTEIN_MIN - 1})
    assert "мало белка" not in _names({"белки": food.PROTEIN_MIN + 1})
    # Доля, а не граммы: 300 г углеводов при 3500 ккал - это норма
    assert "много углеводов" in _names({"калории": 1800, "углеводы": 300})
    assert "много углеводов" not in _names({"калории": 3500, "углеводы": 300})


def test_days_without_the_factor_are_written_too():
    """Без нулей сравнивать не с чем, и связь не найдётся никогда."""
    facts = food.day_factors({"калории": 2000, "белки": 90, "углеводы": 200})
    assert {f.name for f in facts} == {"переел", "недоел", "мало белка", "много углеводов"}
    assert all(f.value == 0.0 for f in facts)


def test_nothing_is_said_about_what_the_export_does_not_have():
    """Нет колонки с белком - нет и суждения о том, много его было или мало."""
    assert {f.name for f in food.day_factors({"калории": 2000})} == {"переел", "недоел"}
    assert food.day_factors({}) == []


def test_reads_a_plain_export():
    table = ("дата,калории,белки,жиры,углеводы\n"
             "2026-08-01,2900,70,95,410\n"
             "2026-08-02,1500,80,50,150\n")
    days = food.read_csv(table)
    assert set(days) == {date(2026, 8, 1), date(2026, 8, 2)}
    assert days[date(2026, 8, 1)]["калории"] == 2900
    assert days[date(2026, 8, 2)]["белки"] == 80


def test_reads_the_russian_excel_dialect():
    """Точка с запятой, запятая в дробях и дата днём вперёд - обычный русский Excel."""
    table = ("дата;калории;белки;жиры;углеводы\n"
             "01.08.2026;2900,5;70;95;410\n")
    days = food.read_csv(table)
    assert days[date(2026, 8, 1)]["калории"] == 2900.5


def test_english_headers_work_too():
    days = food.read_csv("date,calories,protein,fat,carbs\n2026-08-01,2000,70,60,250\n")
    assert days[date(2026, 8, 1)]["углеводы"] == 250


def test_broken_rows_do_not_spoil_the_whole_export():
    """Одна испорченная строка не повод отказывать во всей выгрузке."""
    table = ("дата,калории\n"
             "не дата,2000\n"
             "2026-08-01,ерунда\n"
             "2026-08-02,2000\n")
    assert set(food.read_csv(table)) == {date(2026, 8, 2)}


def test_export_without_a_date_column_is_refused():
    assert food.read_csv("калории,белки\n2000,70\n") == {}
    assert food.read_csv("") == {}


def test_attach_creates_the_day_and_does_not_double_it():
    """КБЖУ - данные про тот день, как и показания кольца: день заводим."""
    timeline = Timeline()
    timeline.add(DayRecord(day=DAY))
    added = food.attach(timeline, {DAY: {"калории": 3000},
                                   date(2026, 8, 2): {"калории": 3000}})
    assert added == 4                      # по два фактора на каждый из двух дней
    assert len(timeline) == 2
    assert timeline.days[0].factor("переел") == 1.0

    # Второй раз те же числа ничего не удваивают
    assert food.attach(timeline, {DAY: {"калории": 3000}}) == 0


def test_numbers_are_shown_back_to_the_person():
    said = food.told({"калории": 2900, "белки": 70, "жиры": 95, "углеводы": 410})
    assert said == "2900 ккал, белки 70 г, жиры 95 г, углеводы 410 г"
