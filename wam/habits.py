"""
Дни без привычки и дни без записей - то, чего человек не сказал.

Без этого модуля главная функция продукта не работает на словах человека.
Разбор речи пишет привычку только тогда, когда она была: «пил пиво» - это
факт, а молчание про пиво - ничто. В итоге у «пива» в дневнике одни единицы,
группы «дней без пива» не существует вовсе, и `find_links` не находит по
привычкам из рассказа ничего - ни с порогом семь, ни с порогом три. Связи
находились только по кольцу и погоде: те пишут и нули.

Здесь мы достраиваем недостающую половину: если человек в этот день писал в
дневник и про свою привычку не упомянул - считаем, что её не было.

Это ДОГАДКА программы, а не слова человека, поэтому:

* нули идут отдельным источником (`SOURCE`), а не «diary» - по факту всегда
  видно, кто его написал, и `basis()` говорит об этом человеку прямым текстом;
* догадываемся только про привычки из личного списка человека - те, что он сам
  хотя бы раз называл. Весь словарь сюда тащить нельзя: «перелёт: не было» за
  каждый день - это выдуманные данные, которых он нам не давал;
* только за дни, когда он вообще писал. В день, когда человек молчал, мы про
  него не знаем ничего и врать не должны;
* и только внутри окна: далёкое прошлое задним числом не переписываем.
"""
from __future__ import annotations

from datetime import date, timedelta

from .schema import DayRecord, Fact, Timeline

# Источник таких нулей. Не «diary»: человек этого не говорил, это наш вывод из
# его молчания.
SOURCE = "implied"

# Источники, которые считаются словами самого человека.
TOLD_SOURCES = ("diary", "")

# Окно, внутри которого достраиваем нули - и по списку привычек, и по дням.
# Полтора месяца: движку на первый вывод нужно недели три (семь дней с
# привычкой и семь без), и запас вдвое даёт ему набрать их с обеих сторон.
# Дальше окно расширять незачем - привычка, которую не называли полтора
# месяца, из жизни человека уже ушла, и проставлять за неё нули задним числом
# значит выдумывать за него прошлое.
WINDOW_DAYS = 45


def told_habits(timeline: Timeline, since: date, until: date) -> set[str]:
    """
    Личный список привычек: то, что человек сам называл в этом окне хотя бы раз.

    Берём только положительные упоминания: «не пил кофе» говорит про этот день,
    но само по себе привычкой в жизни человека кофе не делает.
    """
    names: set[str] = set()
    for record in timeline.days:
        if not since <= record.day <= until:
            continue
        for fact in record.facts:
            if fact.kind == "factor" and fact.value > 0 and fact.source in TOLD_SOURCES:
                names.add(fact.name)
    return names


def wrote_that_day(record: DayRecord) -> bool:
    """
    Писал ли человек в дневник в этот день. Показания кольца и погода за день
    записью не считаются: их приносим мы, а не он.
    """
    return any(fact.source in TOLD_SOURCES for fact in record.facts)


def imply_absences(timeline: Timeline, today: date | None = None) -> int:
    """
    Проставить нули за дни, когда про привычку не вспомнили. Возвращает число
    дописанных фактов - по нему видно, что достройка вообще сработала.

    Ничего не затирает: день, где привычка уже записана - хоть единицей, хоть
    честным нулём со слов человека, - остаётся как был.
    """
    today = today or date.today()
    since = today - timedelta(days=WINDOW_DAYS)

    habits = told_habits(timeline, since, today)
    if not habits:
        return 0

    added = 0
    for record in timeline.days:
        if not since <= record.day <= today:
            continue
        if not wrote_that_day(record):
            continue        # в этот день человек молчал - судить не о чем
        for name in sorted(habits):
            if record.factor(name) is not None:
                continue    # про эту привычку в этот день уже что-то известно
            record.add(Fact("factor", name, 0.0, SOURCE))
            added += 1
    return added


def implied_here(timeline: Timeline, factor: str) -> bool:
    """
    Есть ли среди дней без этой привычки наши догадки. Нужно, чтобы сказать
    человеку честно, на чём держится сравнение.
    """
    for record in timeline.days:
        for fact in record.facts:
            if fact.kind == "factor" and fact.name == factor and fact.source == SOURCE:
                return True
    return False


# ── дни, когда человек не писал вовсе ─────────────────────────────────────

# Дольше этого пропуск перестаёт быть пропуском: человек не «пропал на пару
# дней», а вернулся спустя месяц, и спрашивать его про каждый день бессмысленно.
GAP_LIMIT = 14


def missed_days(timeline: Timeline, today: date) -> list[date]:
    """
    Дни между последней записью и сегодняшним днём, в которые человек не писал.
    Пустой список - пропуска нет или дневник ещё не начинался.

    Сегодняшний день не считаем: он ещё идёт.
    """
    written = [record.day for record in timeline.days
               if record.day < today and wrote_that_day(record)]
    if not written:
        return []
    last = max(written)
    gap = (today - last).days - 1
    if gap <= 0 or gap > GAP_LIMIT:
        return []
    return [last + timedelta(days=step) for step in range(1, gap + 1)]
