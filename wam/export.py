"""
Выгрузка дневника: то, что человек может распечатать и отдать врачу.

Смысл ровно один. На приёме человек говорит «мне плохо, я устал» - и это всё,
что у специалиста есть. А у него в дневнике лежат тридцать дней с цифрами:
когда началось, что при этом было, что показывало кольцо. Разница между этими
двумя разговорами и есть весь продукт.

Форматов два, и оба нужны:

* **текст** - то, что читается глазами и кладётся на стол. Дни подряд, каждый
  четырьмя строками, без таблиц и без вёрстки: такой файл откроется где угодно
  и распечатается как есть;
* **CSV** - то же самое таблицей, для Excel и для передачи в другую программу.
  Разделитель - точка с запятой, дробные через запятую, в начале BOM: русский
  Excel открывает такой файл сразу, а UTF-8 без BOM показывает кракозябрами.

Выводов программы в файле нет - ни связей, ни отклонений, ни догадок про дни
без привычки (`habits.SOURCE`). В выгрузке только то, что человек записал сам,
и то, что измерил его прибор. Наши рассуждения специалисту не нужны, а вот
выдать их за данные было бы нечестно.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from .derive import DEVICE_SOURCES
from .habits import SOURCE as IMPLIED
from .questions import as_told
from .schema import DayRecord, Fact, Timeline

# Порядок показателей в файле: сперва то, о чём человек говорит сам, потом
# показания прибора. Алфавит поставил бы «активность» впереди «энергии», а
# читают такой файл сверху вниз.
_METRIC_ORDER = ("энергия", "настроение", "тревога", "качество сна", "часов сна",
                 "головная боль", "стресс", "активность", "пульс покоя",
                 "вариабельность пульса", "глубокий сон", "быстрый сон",
                 "пробуждения за ночь", "температура тела", "сатурация")

# Показатели, которые человек называет в обратную сторону: тревога 8 - это
# сильная тревога. Внутри программы они хранятся наоборот, `questions.as_told`
# переводит обратно. В заголовке колонки это надо подписать, иначе восьмёрка
# читается как хороший день.
_STRENGTH = ("тревога", "головная боль", "стресс")

_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг",
             "пятница", "суббота", "воскресенье")

# Пояснение к цифрам. Идёт в начало текстовой выгрузки: без него десятка в
# строке «тревога» означает что угодно.
_LEGEND = """Это дневник, который человек вёл сам. Диагнозов, назначений и результатов
анализов тут нет и быть не может - только записи про дни и показания носимого
устройства.

Все оценки - по шкале от 0 до 10, в том виде, в каком о них говорят: энергия,
сон, настроение, активность - чем больше, тем лучше; тревога, головная боль,
напряжение - чем больше, тем сильнее. Показания прибора приведены к той же
шкале, поэтому исходных единиц - ударов в минуту, миллисекунд, часов - в этой
выгрузке нет."""


def as_text(timeline: Timeline, made_on: date | None = None) -> str:
    """Дневник как читаемый текст: шапка с пояснением и дни подряд."""
    days = sorted(timeline.days, key=lambda record: record.day)
    made_on = made_on or date.today()
    out = [_header(days, made_on), _LEGEND, ""]
    if not days:
        return "\n".join(out)

    for record in days:
        out.append(f"{_date(record.day)}, {_WEEKDAYS[record.day.weekday()]}")
        out += [f"  {line}" for line in _day_lines(record)]
        out.append("")
    return "\n".join(out)


def as_csv(timeline: Timeline) -> str:
    """
    Дневник таблицей. Строка на день, колонка на показатель.

    Пояснений и пустых строк в таблице нет: Excel читает первую строку как
    заголовок, и всё, что стоит выше, ломает разбор колонок.
    """
    days = sorted(timeline.days, key=lambda record: record.day)
    metrics = _metric_columns(days)
    titles = [name + " (сила)" if name in _STRENGTH else name for name in metrics]

    buffer = io.StringIO()
    table = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    table.writerow(["дата", "день недели", *titles, "было", "события", "с прибора"])
    for record in days:
        told = {fact.name: fact for fact in record.facts if fact.kind == "metric"}
        table.writerow([
            _date(record.day),
            _WEEKDAYS[record.day.weekday()],
            *(_number(as_told(name, told[name].value)) if name in told else ""
              for name in metrics),
            ", ".join(_habits(record)),
            ", ".join(_events(record)),
            ", ".join(name for name, fact in told.items()
                      if fact.source in DEVICE_SOURCES),
        ])
    # BOM в начале: без него русский Excel открывает UTF-8 как кракозябры, и
    # файл, ради которого всё затевалось, человек не сможет никому показать.
    # Записан escape-последовательностью: сам знак в исходнике невидим, и
    # стереть его случайной правкой было бы слишком легко.
    return "\ufeff" + buffer.getvalue()


def _header(days: list[DayRecord], made_on: date) -> str:
    if not days:
        return ("Дневник самонаблюдения\n\n"
                f"Записей пока нет. Выгружено {_date(made_on)}.\n")
    return ("Дневник самонаблюдения\n\n"
            f"Записи с {_date(days[0].day)} по {_date(days[-1].day)}, "
            f"дней с записями: {len(days)}.\n"
            f"Выгружено {_date(made_on)}.\n")


def _day_lines(record: DayRecord) -> list[str]:
    """Один день: что человек сказал, что было и что показал прибор."""
    lines = []
    told = [fact for fact in record.facts
            if fact.kind == "metric" and fact.source not in DEVICE_SOURCES]
    measured = [fact for fact in record.facts
                if fact.kind == "metric" and fact.source in DEVICE_SOURCES]

    if told:
        lines.append("самочувствие: " + ", ".join(
            f"{fact.name} {_number(as_told(fact.name, fact.value))}"
            for fact in _ordered(told)))
    habits = _habits(record)
    if habits:
        lines.append("было: " + ", ".join(habits))
    events = _events(record)
    if events:
        lines.append("события: " + ", ".join(events))
    if measured:
        lines.append("с прибора: " + ", ".join(
            f"{fact.name} {_number(as_told(fact.name, fact.value))}"
            for fact in _ordered(measured)))
    if record.raw_text:
        lines.append(f"своими словами: {record.raw_text}")
    return lines or ["записей за этот день нет"]


def _habits(record: DayRecord) -> list[str]:
    """
    Что в этот день было: привычки, еда, погода - всё, что случилось на самом
    деле. Факторы, посчитанные из показаний прибора, сюда не идут: они и так
    видны числами в той же строке, а «мало спал» рядом с «качество сна 3»
    выглядит как второе, независимое наблюдение.

    Догадки про дни без привычки (`habits.SOURCE`) не идут тем более: их
    человек нам не говорил.
    """
    return [fact.name for fact in record.facts
            if fact.kind == "factor" and fact.value > 0
            and fact.source not in DEVICE_SOURCES and fact.source != IMPLIED]


def _events(record: DayRecord) -> list[str]:
    return [fact.name for fact in record.facts if fact.kind == "event"]


def _ordered(facts: list[Fact]) -> list[Fact]:
    """Показатели в привычном порядке, незнакомые - в конец, по алфавиту."""
    return sorted(facts, key=lambda fact: (_place(fact.name), fact.name))


def _metric_columns(days: list[DayRecord]) -> list[str]:
    names = {fact.name for record in days for fact in record.facts
             if fact.kind == "metric"}
    return sorted(names, key=lambda name: (_place(name), name))


def _place(name: str) -> int:
    return _METRIC_ORDER.index(name) if name in _METRIC_ORDER else len(_METRIC_ORDER)


def _date(day: date) -> str:
    """
    День как 20.08.2026: так его пишут в русских документах, и так его читает
    Excel. ISO-запись он оставил бы текстом, и сортировка по дате не работала бы.
    """
    return day.strftime("%d.%m.%Y")


def _number(value: float) -> str:
    """Оценка по-русски: без хвостового нуля у целых, с запятой у дробных."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")
