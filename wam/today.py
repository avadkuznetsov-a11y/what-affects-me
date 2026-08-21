"""
Сегодняшний день на фоне остальных.

Связи из `insights` появляются не раньше, чем накопится по семь дней с
привычкой и без неё - это честно, но означает, что первые недели дневник
молчит. А смысл продукта в том, чтобы он смотрел на жизнь человека каждый
день, а не ждал статистики.

Здесь то, что можно сказать уже на нескольких днях и не соврать:

* прибор и рассказ про один и тот же день - совпали или разошлись;
* закономерность на малых данных: дни с привычкой против дней без неё,
  порог всего три и три, и потому это называется наблюдением, а не выводом;
* как сегодняшние показатели выглядят на фоне обычных;
* как часто повторяется названная привычка.

Тон везде один: наблюдение, а не диагноз. Про здоровье мы не говорим ничего -
только про то, что видно в данных человека.
"""
from __future__ import annotations

from datetime import date

from .derive import DEVICE_SOURCES
from .insights import Link
from .phrases import factor_phrase, metric_phrase
from .schema import DayRecord, Timeline

# Меньше этого сравнивать не с чем: один-два дня «обычным» не назовёшь.
MIN_DAYS_TO_COMPARE = 3

# Насколько показатель должен отличаться от обычного, чтобы про это стоило
# говорить. Шкала десятибалльная, полбалла - шум.
NOTABLE_GAP = 1.0

# Сколько наблюдений говорим за раз. Человек написал одну фразу - в ответ
# несколько строк, а не разбор его жизни. Всё остальное подождёт до завтра.
MAX_LINES = 2

# Что считать сказанным «высоко» и «низко» по десятибалльной шкале.
SAID_HIGH = 7.0
SAID_LOW = 4.0

# Наблюдение показываем, только когда разрыв заметен глазами и не похож на
# случайность. Поправку на число проверенных пар здесь НЕ применяем: с ней на
# трёх днях не проходит ничего. Расплата за это - оговорка в самой фразе, и
# убирать её нельзя.
HINT_EFFECT = 1.0
HINT_P = 0.1

# Как назвать показатель, когда он выше или ниже обычного
_BETTER = {
    "качество сна": "спалось лучше обычного",
    "энергия": "сил больше обычного",
    "тревога": "спокойнее обычного",
    "настроение": "настроение лучше обычного",
    "головная боль": "голова болела меньше обычного",
}
_WORSE = {
    "качество сна": "спалось хуже обычного",
    "энергия": "сил меньше обычного",
    "тревога": "тревожнее обычного",
    "настроение": "настроение хуже обычного",
    "головная боль": "голова болела сильнее обычного",
}

# Как назвать показание прибора внутри фразы «кольцо показало ...».
_DEVICE_PHRASE = {
    "высокий стресс": "высокий стресс",
    "высокий пульс": "пульс выше обычного",
    "мало спал": "короткий сон",
    "хорошо выспался": "хороший сон",
    "мало двигался": "почти без движения",
    "много двигался": "много движения",
}

# Прибор против рассказа: что человек сказал про себя и что в тот же день
# показало кольцо. Каждая строка - (показатель из рассказа, «высоко» или
# «низко», какие показания прибора должны при этом сойтись, как это сказать).
#
# Порядок важен: говорим про первое, что сошлось, и на этом останавливаемся.
# Сначала расхождения - ради них всё и затевалось: человек не видит того, что
# видит прибор. Совпадения идут последними и говорятся одной строкой.
#
# Формулировки намеренно без причин и без здоровья: «кольцо показало высокий
# стресс» - это факт из данных, а «у вас стресс» - уже диагноз, которого мы
# ставить не имеем права.
_VS_DEVICE: list[tuple[str, str, tuple[str, ...], str]] = [
    # Оценку тревоги в этой строке не называем: внутри программы шкала
    # перевёрнута (девять означает спокойный день), и цифра рядом со словом
    # «спокойный» человеком читается наоборот.
    ("тревога", "высоко", ("высокий стресс", "высокий пульс"),
     "Вы описали день как спокойный, а кольцо показало {what}. "
     "Это не спор, просто два взгляда на один день."),
    ("настроение", "высоко", ("высокий стресс", "высокий пульс"),
     "Настроение вы оценили высоко - {score} из 10, а кольцо показало {what}. "
     "Это не спор, просто два взгляда на один день."),
    ("энергия", "высоко", ("высокий стресс", "высокий пульс"),
     "Сил, по вашим словам, хватало - {score} из 10, а кольцо показало {what}. "
     "Это не спор, просто два взгляда на один день."),
    ("энергия", "низко", ("мало спал",),
     "Сил мало - {score} из 10, и кольцо показало {what}. Пока это совпадение "
     "в один день; повторится - будет видно связь."),
    ("настроение", "низко", ("мало спал",),
     "Настроение вы оценили низко - {score} из 10, и кольцо показало {what}. "
     "Пока это совпадение в один день; повторится - будет видно связь."),
    ("энергия", "низко", ("хорошо выспался",),
     "Сил мало - {score} из 10, хотя кольцо показало {what}. "
     "Значит, дело не в этой ночи - посмотрим, в чём."),
    ("энергия", "высоко", ("хорошо выспался",),
     "Сил много - {score} из 10, и кольцо согласно: показало {what}."),
]


def observations(timeline: Timeline, today: DayRecord,
                 habits: set[str] | frozenset[str],
                 hints: list[Link] | None = None) -> list[str]:
    """
    Чем сегодняшний день отличается от прежних. Пустой список - сказать нечего.

    hints - связи, посчитанные с пониженным порогом (`insights.find_links`
    с `min_days`). Своей статистики тут нет и быть не должно: считает её один
    движок, здесь только слова.

    Сравниваем с днями ДО сегодняшнего: иначе сегодняшнее значение попадёт и в
    «обычное», и разница смажется.
    """
    # Прибор и рассказ спорят про один день, история для этого не нужна -
    # сказать об этом можно и на первой же записи.
    lines = _device_lines(today)

    past = [d for d in timeline.days if d.day != today.day]
    if len(past) < MIN_DAYS_TO_COMPARE:
        return lines[:MAX_LINES]

    told, covered = _pattern_lines(past, hints or [], habits, today.day)
    lines += told
    if len(lines) < MAX_LINES:
        lines += _metric_lines(past, today)
    if len(lines) < MAX_LINES:
        lines += _habit_lines(past, set(habits) - covered, today.day)
    return lines[:MAX_LINES]


def _device_lines(today: DayRecord) -> list[str]:
    """
    Что сказал человек и что показал прибор про один и тот же день.

    Больше одной строки не отдаём: два наблюдения про сегодня - это уже разбор,
    а человек всего лишь рассказал, как прошёл день.
    """
    for metric, side, factors, template in _VS_DEVICE:
        score = _told_by_person(today, metric)
        if score is None:
            continue
        fits = score >= SAID_HIGH if side == "высоко" else score <= SAID_LOW
        if not fits:
            continue
        shown = [_DEVICE_PHRASE[name] for name in factors if _device_shows(today, name)]
        if not shown:
            continue
        return [template.format(score=f"{score:g}", what=" и ".join(shown))]
    return []


def _told_by_person(record: DayRecord, metric: str) -> float | None:
    """
    Оценка показателя, которую человек назвал сам. None - он про это не
    говорил. Сверять прибор с ним же смысла нет: там всегда сойдётся.
    """
    for fact in record.facts:
        if fact.kind == "metric" and fact.name == metric:
            return None if fact.source in DEVICE_SOURCES else fact.value
    return None


def _device_shows(record: DayRecord, factor: str) -> bool:
    """Показал ли прибор этот фактор сегодня."""
    for fact in record.facts:
        if fact.kind == "factor" and fact.name == factor:
            return fact.value > 0 and fact.source in DEVICE_SOURCES
    return False


def _pattern_lines(past: list[DayRecord], hints: list[Link],
                   habits: set[str] | frozenset[str],
                   today: date) -> tuple[list[str], set[str]]:
    """
    Закономерность на малых данных про то, что человек назвал сейчас.

    Возвращает строки и привычки, про которые уже сказано: повторять про них
    ещё и «уже третий раз» незачем, это то же самое другими словами.

    Берём одну, самую убедительную: перечислять человеку три полудогадки -
    верный способ, чтобы он перестал верить всем сразу.
    """
    ranked = sorted(hints, key=lambda l: (l.p_value, -abs(l.effect)))
    for link in ranked:
        if link.factor not in habits:
            continue
        if abs(link.effect) < HINT_EFFECT or link.p_value > HINT_P:
            continue
        return [_pattern_line(past, link, today)], {link.factor}
    return [], set()


def _pattern_line(past: list[DayRecord], link: Link, today: date) -> str:
    """
    Одна фраза про наблюдение. Оговорка в конце обязательна: три дня против
    трёх - это не вывод, и человек должен знать об этом от нас, а не выяснить
    сам через месяц.
    """
    # «После кофе» в начале фразы, «после кофе» в середине - строчная буква
    start = factor_phrase(link.factor)
    start = start[0].lower() + start[1:]
    if "," in start:
        start += ","        # придаточное надо закрыть, иначе фраза читается сплошняком
    when = {0: "", 1: " на следующий день", 2: " через день"}[link.lag_days]
    change = metric_phrase(link.metric, worse=link.effect < 0)

    said = (f"Замечено: {start}{when} {change} - {_number(link.value_with)} против "
            f"{_number(link.value_without)}. Пока это наблюдение, не вывод.")

    # Сколько раз это уже было - тем и живёт наблюдение: «пиво третий раз за
    # неделю» человек проверяет по своей же памяти и сразу понимает, о чём речь.
    often = _habit_line(past, link.factor, today)
    return f"{often} {said}" if often else said


def _metric_lines(past: list[DayRecord], today: DayRecord) -> list[str]:
    """Показатели сегодня против обычного."""
    lines = []
    for fact in today.facts:
        if fact.kind != "metric":
            continue
        values = [d.metric(fact.name) for d in past]
        values = [v for v in values if v is not None]
        if len(values) < MIN_DAYS_TO_COMPARE:
            continue
        usual = sum(values) / len(values)
        gap = fact.value - usual
        if abs(gap) < NOTABLE_GAP:
            continue
        words = _BETTER if gap > 0 else _WORSE
        # У тревоги и головной боли шкала перевёрнута: больше - хуже
        if fact.name in ("тревога", "головная боль"):
            words = _WORSE if gap > 0 else _BETTER
        phrase = words.get(fact.name)
        if not phrase:
            continue
        lines.append(f"Сегодня {phrase}: {fact.value:g} против обычных "
                     f"{usual:.1f} за {_days(len(values))}.".replace(".0 ", " "))
    return lines


def _habit_lines(past: list[DayRecord], habits: set[str] | frozenset[str],
                 today: date) -> list[str]:
    """Как часто повторяется то, что человек назвал сегодня."""
    lines = []
    for habit in sorted(habits):
        line = _habit_line(past, habit, today)
        if line:
            lines.append(line)
    return lines


def _habit_line(past: list[DayRecord], habit: str, today: date) -> str:
    """Одна привычка: сколько раз и когда была прошлый раз. Пусто - было мало."""
    days_with = [d for d in past if (d.factor(habit) or 0) > 0]
    if len(days_with) < 2:
        return ""
    last = max(d.day for d in days_with)
    ago = (today - last).days
    when = "вчера" if ago == 1 else f"{_days(ago)} назад"
    return f"«{habit}» уже {_times(len(days_with) + 1)}, прошлый раз {when}."


def _number(value: float) -> str:
    """Балл по-русски: с запятой, а не с точкой."""
    return f"{value:.1f}".replace(".", ",")


def _days(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return f"{count} дней"
    last = count % 10
    if last == 1:
        return f"{count} день"
    if 2 <= last <= 4:
        return f"{count} дня"
    return f"{count} дней"


def _times(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return f"{count} раз"
    last = count % 10
    if last == 1:
        return f"{count} раз"
    if 2 <= last <= 4:
        return f"{count} раза"
    return f"{count} раз"
