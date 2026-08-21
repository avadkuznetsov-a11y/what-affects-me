"""
Еда как источник фактов о дне - числами.

Словами про еду человек рассказывает сам, и это разбирает `extract.py`: «ел
много мяса», «фастфуд на обед». Но есть и вторая половина, которую словами не
передать: сколько всего съедено. Её человек уже считает - в приложении для
подсчёта КБЖУ, - и оттуда её можно взять готовой.

Формат нарочно самый простой, какой отдают все такие приложения: строка на
день, пять колонок - дата, калории, белки, жиры, углеводы. Ни к одному
конкретному приложению не привязываемся: у каждого свой личный кабинет и свои
правила выгрузки, а CSV умеют все.

Из чисел получаются такие же факторы дня, как «кофе» или «жара»: переел,
недоел, мало белка, много углеводов. Дальше движок связей работает с ними
одинаково и не знает, откуда они пришли.

Про здоровье тут не говорится ничего. «Переел» - это про то, что калорий
вышло заметно больше обычной суточной нормы, а не про то, вредно это или
полезно: такого мы не знаем и знать не можем.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from .schema import DayRecord, Fact, Timeline

SOURCE = "food"

# ── Пороги ────────────────────────────────────────────────────────────────
# Все они взяты от обычной суточной нормы взрослого человека, а не придуманы.
# Норма зависит от пола, веса и подвижности, которых мы не знаем, поэтому
# пороги грубые и с запасом: фактор должен ловить заметный перекос, а не
# отклонение на сотню килокалорий.

# Суточная норма энергии взрослого: около 2000 ккал для женщин и 2500 для
# мужчин (нормы физиологических потребностей, МР 2.3.1.2432-08). Пола мы не
# знаем - берём середину.
CALORIES_NORM = 2300.0

# Четверть сверх нормы - это уже заметно больше, чем обычный день.
OVEREAT = 1.25

# Четверть ниже нормы - столько выходит, когда человек пропустил еду, а не
# просто съел поменьше.
UNDEREAT = 0.75

# Белок: нижняя граница нормы - 0,8 грамма на килограмм веса, то есть около
# 60 граммов для человека весом 75 кг. Веса мы не знаем, поэтому берём именно
# нижнюю границу: ниже неё мало у кого.
PROTEIN_MIN = 60.0

# Углеводы дают 50-55% суточной калорийности - это и есть норма. Выше 60% -
# перекос, ради которого фактор и заводится.
CARB_SHARE = 0.60

# Сколько килокалорий в грамме углеводов - школьная физиология, не порог.
CARB_CALORIES = 4.0

# Как называются колонки. Приложения выгружают по-разному и по-русски, и
# по-английски; сводим к общему словарю, иначе разбор разъедется на первой же
# чужой выгрузке.
COLUMNS: dict[str, str] = {
    "дата": "день", "день": "день", "date": "день", "day": "день",
    "калории": "калории", "ккал": "калории", "энергия": "калории",
    "calories": "калории", "kcal": "калории", "energy": "калории",
    "белки": "белки", "белок": "белки", "protein": "белки", "proteins": "белки",
    "жиры": "жиры", "жир": "жиры", "fat": "жиры", "fats": "жиры",
    "углеводы": "углеводы", "углеводов": "углеводы", "carbs": "углеводы",
    "carbohydrates": "углеводы",
}

NUMBERS = ("калории", "белки", "жиры", "углеводы")

# Строк больше, чем в году, ни одна выгрузка за раз не даёт, а память у
# прототипа общая на всех.
MAX_ROWS = 400


def day_factors(values: dict[str, float]) -> list[Fact]:
    """
    Факторы дня из чисел КБЖУ.

    Фактор ставим и когда условия не было (значение 0): без дней «без фактора»
    сравнивать не с чем, и связь не найдётся никогда. А про то, чего в
    выгрузке нет, не говорим ничего: нет колонки с белком - нет и суждения о
    том, много его было или мало.
    """
    facts: list[Fact] = []

    def put(name: str, hit: bool) -> None:
        facts.append(Fact("factor", name, 1.0 if hit else 0.0, SOURCE))

    calories = values.get("калории")
    if calories is not None:
        put("переел", calories >= CALORIES_NORM * OVEREAT)
        put("недоел", calories <= CALORIES_NORM * UNDEREAT)

    protein = values.get("белки")
    if protein is not None:
        put("мало белка", protein < PROTEIN_MIN)

    carbs = values.get("углеводы")
    # Доля, а не граммы: 300 граммов углеводов при 3500 ккал - это норма, а при
    # 1800 ккал - перекос, и один порог в граммах их не различает.
    if carbs is not None and calories:
        put("много углеводов", carbs * CARB_CALORIES >= calories * CARB_SHARE)

    return facts


def read_csv(text: str) -> dict[date, dict[str, float]]:
    """
    Разобрать выгрузку. Возвращает числа по дням; кривые строки молча
    пропускаем - одна испорченная строка не повод отказывать во всей выгрузке.

    Разделитель бывает и запятой, и точкой с запятой: русский Excel сохраняет
    вторым, и тогда дробные числа в файле идут через запятую.
    """
    # Метку порядка байтов Excel ставит в начало файла, и без неё первая
    # колонка не узнаётся - «﻿дата» это не «дата».
    text = (text or "").lstrip("﻿").strip()
    if not text:
        return {}

    head = text.splitlines()[0]
    semicolons = head.count(";") > head.count(",")
    delimiter = ";" if semicolons else ","

    rows = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(rows)
    except StopIteration:
        return {}

    columns = [COLUMNS.get(name.strip().lower(), "") for name in header]
    if "день" not in columns:
        return {}

    out: dict[date, dict[str, float]] = {}
    for row in rows:
        if len(out) >= MAX_ROWS:
            break
        values: dict[str, float] = {}
        day: date | None = None
        for column, cell in zip(columns, row):
            cell = cell.strip()
            if not column or not cell:
                continue
            if column == "день":
                day = _as_date(cell)
            else:
                number = _as_number(cell, semicolons)
                if number is not None:
                    values[column] = number
        if day is not None and values:
            out[day] = values
    return out


def attach(timeline: Timeline, by_day: dict[date, dict[str, float]]) -> int:
    """
    Дописать факторы еды в дневник. Возвращает число добавленных фактов.

    В отличие от погоды, новые дни тут заводим: КБЖУ - это данные ровно про
    тот день, как и показания кольца, и выгрузка за месяц назад имеет смысл
    даже там, где человек ничего не рассказывал. Так же поступает
    `wearables.merge_into`.
    """
    known = {record.day: record for record in timeline.days}
    added = 0
    for day, values in by_day.items():
        record = known.get(day)
        if record is None:
            record = DayRecord(day=day)
            timeline.add(record)
            known[day] = record
        for fact in day_factors(values):
            if record.factor(fact.name) is not None:
                continue        # уже посчитано за этот день
            record.add(fact)
            added += 1
    return added


def told(values: dict[str, float]) -> str:
    """Числа дня строкой - чтобы человек видел, что именно мы записали."""
    names = {"калории": "{:g} ккал", "белки": "белки {:g} г",
             "жиры": "жиры {:g} г", "углеводы": "углеводы {:g} г"}
    said = [names[name].format(values[name]) for name in NUMBERS if name in values]
    return ", ".join(said)


def _as_number(cell: str, comma_is_decimal: bool) -> float | None:
    """Число из ячейки. Пробелы внутри числа ставит Excel, а не человек."""
    cell = cell.replace(" ", "").replace(" ", "")
    if comma_is_decimal:
        cell = cell.replace(",", ".")
    try:
        value = float(cell)
    except ValueError:
        return None
    # Отрицательных калорий не бывает, а вот опечатка бывает: такому числу
    # верить нельзя, и лучше не знать значения вовсе, чем знать неправильное.
    return value if value >= 0 else None


def _as_date(cell: str) -> date | None:
    """
    Дата из ячейки. Кроме обычного 2026-08-01 принимаем 01.08.2026: так дату
    пишет русский Excel, и выгрузка из него иначе не разбиралась бы вовсе.
    """
    try:
        return date.fromisoformat(cell[:10])
    except ValueError:
        pass
    parts = cell.strip().split(".")
    if len(parts) == 3:
        try:
            return date(int(parts[2]), int(parts[1]), int(parts[0]))
        except ValueError:
            return None
    return None
