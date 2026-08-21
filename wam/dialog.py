"""
Один шаг разговора - общий для страницы и для мессенджера.

Человек пишет фразу, мы разбираем её на факты, дописываем в дневник, задаём один
вопрос, если без него не получается вывод, и говорим, что уже известно. Логика
одна и та же независимо от того, откуда пришла фраза: со страницы или из чата.
Разное только оформление - страница отдаёт сообщения как JSON, бот собирает из
них текст.

Разборщик передаётся аргументом и собирается лениво: если выбирать модель в
момент импорта, любой тест на машине с ключом в окружении полезет в сеть.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .derive import DEVICE_SOURCES, derive_factors
from .diary import Diary
from .extract import (LLMExtractor, RuleExtractor, day_mentioned,
                      measured_number)
from .habits import missed_days
from .insights import Link
from .llm import available_engine
from .phrases import basis, next_step, say
from .questions import (NO_STATE, NOTHING_UNDERSTOOD, apply_answer, day_name,
                        detail_of, gap_question, metric_of,
                        next_question, score_in)
from .schema import DayRecord, Fact
from .today import observations
from .wearables import SberRingSource, merge_into
from . import weather

# Сколько уточнений за день. Вопрос - это не анкета, а способ разобраться: без
# «сколько выпили» и «долго ли гуляли» связь получается грубой. Ограничение
# всё же есть: подряд на одну реплику мы задаём ровно один вопрос, повторно
# один и тот же не задаём никогда, и на день их всё-таки не десяток.
MAX_QUESTIONS = 6

# Длиннее этого реплика - уже рассказ про день, а не оценка в ответ на вопрос.
MAX_ANSWER_WORDS = 8

# Пока дней в дневнике меньше, про отсутствие выводов лучше молчать: сказать
# «данных мало» человеку, который ведёт дневник второй день, - значит повторять
# очевидное на каждую фразу.
MIN_DAYS_FOR_HINT = 7

# Приветствие и благодарность - не рассказ про день, но и не белиберда.
# Отвечать на «привет» подсказкой «не понял, что записать» - самый быстрый
# способ показать человеку, что перед ним не собеседник, а форма ввода.
_GREETING = re.compile(r"(привет\w*|здравствуй\w*|здрасьте|доброго\s+\w+|"
                       r"добр(?:ое|ый|ой)\s+(?:утр\w+|день|вечер|ноч\w+)|"
                       r"хай|ку|салют|доброй\s+ночи)\b")
_THANKS = re.compile(r"(спасибо|спс|благодар\w*|пасиб\w*)\b")

GREETING_REPLY = ("Здравствуйте. Расскажите, как прошёл день - что делали и как "
                  "себя чувствовали.")
THANKS_REPLY = "Пожалуйста. Будет что записать - пишите."

# Короткий отказ на случай, когда вопрос уже висит: повторять его слово в слово
# нельзя, а молчать в ответ на непонятую фразу - невежливо.
DID_NOT_GET_IT = "Не понял. Скажите обычными словами, что было и как вы себя чувствовали."

# «А как вы себя чувствовали?» спрашивает не про конкретный показатель, поэтому
# голая оценка к нему не привязывается: семь - это про что?
WHICH_METRIC = ("Семь - это про что: сон, тревога, силы, настроение? "
                "Можно и словами: «выспался», «разбитый», «спокойный день».")

# Оценка без единого слова про самочувствие: «7», «на 7», «баллов 7».
_BARE_SCORE = re.compile(r"^(?:на\s+)?(?:10|\d)(?:\s*(?:из\s*10|балл\w*))?$")


def _bare_score(text: str) -> bool:
    return bool(_BARE_SCORE.match(text.strip()))


# Ответ на вопрос о детали - короткая реплика вроде «две чашки, последняя в
# пять» или «часа полтора». Длинный рассказ деталью не считаем: человек просто
# продолжил про день, и разбирать это надо обычным путём.
MAX_DETAIL_WORDS = 12


def _polite(text: str) -> bool:
    """
    Вежливость: человек поздоровался или поблагодарил, а не пытался ответить.

    Отговорки («ну не знаю», «ладно») сюда НЕ входят: это отказ отвечать, и
    вопрос от них должен гаснуть - иначе бот превращается в зануду.
    """
    lowered = text.strip().lower()
    return bool(_GREETING.match(lowered) or _THANKS.match(lowered))


def _looks_like_detail(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered or len(lowered.split()) > MAX_DETAIL_WORDS:
        return False
    # Приветствие и благодарность - это вежливость, а не ответ на вопрос.
    # Пока их принимали за деталь, «привет» после вопроса про кофе записывался
    # как уточнение к кофе, и человек видел «Записал: кофе» на своё «привет».
    if _GREETING.match(lowered) or _THANKS.match(lowered):
        return False
    # Как и невнятное «ага», «ну не знаю» - согласие, а не мера
    return not _VAGUE.match(lowered)


# Реплики, которые ничего не сообщают: человек тянет время или просто
# откликается. Деталью привычки они быть не могут.
_VAGUE = re.compile(r"^(ага|угу|ок(ей)?|да|нет|не знаю|ну не знаю|"
                    r"наверное|потом|ладно|понял\w*|хорошо)\b")


def _remember_detail(record: DayRecord, factor: str, text: str) -> Fact | None:
    """
    Дописать деталь к факту привычки. Возвращает обновлённый факт или None,
    если такой привычки в дне почему-то нет.

    Деталь держим текстом рядом с фактом: «две чашки, последняя в пять» точнее
    любой цифры, которую мы бы из неё вытащили, а для поиска связей важно само
    наличие привычки, оно уже записано.
    """
    import dataclasses

    detail = text.strip()
    for index, fact in enumerate(record.facts):
        if fact.kind == "factor" and fact.name == factor and fact.value > 0:
            # Факт неизменяемый - собираем новый с дописанной деталью
            quote = f"{fact.quote} / {detail}" if fact.quote else detail
            updated = dataclasses.replace(fact, quote=quote)
            record.facts[index] = updated
            return updated
    return None


class Parser:
    """
    Разбор речи: сначала модель, при сбое или пустом ответе - правила.

    Правила не заглушка, а страховка: продукт не должен вставать из-за
    недоступного сервиса.
    """

    def __init__(self, engine: str = "правила", complete=None) -> None:
        self.engine = engine
        self.rules = RuleExtractor()
        self.model = LLMExtractor(complete) if complete and engine != "правила" else None

    @classmethod
    def from_environment(cls) -> "Parser":
        """Модель, для которой в окружении есть ключ, иначе правила."""
        engine, complete = available_engine()
        return cls(engine, complete)

    def parse(self, text: str, day: date) -> DayRecord:
        """
        Модель и правила дополняют друг друга, а не заменяют.

        Раньше ответ модели принимался целиком, как только в нём был хоть один
        факт, и правила уже не смотрели фразу. Модель отвечает неодинаково: во
        фразе «пил кофе в пять, спал часов пять, тревожно» она то возвращает
        тревогу, то нет - и тогда тревога терялась, хотя правила её видят.
        Поэтому берём разбор модели и дописываем то, что она пропустила.
        """
        by_rules = self.rules.extract(text, day)
        if self.model is None:
            return by_rules

        try:
            record = self.model.extract(text, day)
        except Exception:
            return by_rules
        if not record.facts:
            return by_rules

        # Что модель уже назвала - её и оставляем: она понимает живую речь
        # лучше словаря. Правила добавляют только пропущенное.
        known = {(f.kind, f.name) for f in record.facts}
        for fact in by_rules.facts:
            if (fact.kind, fact.name) not in known:
                record.add(fact)
        return record


_DEFAULT: Parser | None = None


def default_parser() -> Parser:
    """Разборщик по умолчанию. Собирается при первом обращении, не при импорте."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Parser.from_environment()
    return _DEFAULT


def parse_day(text: str, day: date, parser: Parser | None = None) -> DayRecord:
    return (parser or default_parser()).parse(text, day)


# Внутри программы шкала одна: больше - лучше самочувствие, поэтому стресс
# хранится перевёрнутым (2,8 означает высокий стресс). Считать так удобно, а
# читать - нет: рядом стояли «высокий стресс (с кольца)» и «стресс 2,8 из 10»,
# и это выглядело как противоречие. Человеку показываем в привычную сторону.
# Показываем в ту же сторону, в какую человек про это говорит. Список один
# на программу - в questions.ASKED_INVERTED, чтобы вопрос, ответ и показ не
# разъехались: именно из-за такого расхождения «тревога на 8» записывалась
# как спокойный день.
from .questions import ASKED_INVERTED as _TOLD_INVERTED


def _as_told(name: str, value: float) -> tuple[str, float]:
    """Как показать показатель человеку: имя и значение в привычную сторону."""
    if name in _TOLD_INVERTED:
        return name, round(10 - value, 1)
    return name, value


def facts_note(facts: list[Fact]) -> str:
    """
    Показываем только то, что было, и сами показатели. Строки вида
    «много двигался: не было» человеку не нужны - это шум.

    На вход идут факты одной реплики, а не вся запись за день: пока сюда
    отдавали день целиком, на каждую фразу бот перечислял всё накопленное с
    утра, в том числе когда в самой фразе не понял ничего.
    """
    habits, metrics = [], []
    for fact in facts:
        if fact.kind == "event":
            continue        # события в поиске связей не участвуют, показывать их незачем
        mark = " (с кольца)" if fact.source in DEVICE_SOURCES else ""
        if fact.kind == "factor":
            if fact.value > 0:
                habits.append(f"{fact.name}{mark}")
            elif fact.source in ("diary", ""):
                # «Не было» показываем, только когда это сказал сам человек.
                # Нули, посчитанные нами - с прибора, из погоды, из КБЖУ или
                # достроенные по молчанию, - для него шум: он их не называл.
                habits.append(f"{fact.name}: не было")
        else:
            name, value = _as_told(fact.name, fact.value)
            metrics.append(f"{name} {value:g} из 10{mark}")

    lines = []
    if habits:
        lines.append("Привычки: " + ", ".join(dict.fromkeys(habits)))
    if metrics:
        lines.append("Самочувствие: " + ", ".join(dict.fromkeys(metrics)))
    return "\n".join(lines)


def step(diary: Diary, text: str, ring: dict | None = None,
         links: list[Link] | None = None, parser: Parser | None = None,
         origin: str = "page", links_from_demo: bool = False,
         hints: list[Link] | None = None) -> list[dict]:
    """
    Шаг разговора и запись дневника на диск.

    Сохранение стоит здесь, а не у того, кто зовёт шаг: выходов из шага с
    десяток (вопрос задан, итог подведён, фраза не понята), и любой забытый
    означал бы потерянную запись. Запись дешёвая - на диск уходит только
    изменившийся день, - поэтому лишний вызов ничего не стоит.
    """
    said = _step(diary, text, ring=ring, links=links, parser=parser, origin=origin,
                 links_from_demo=links_from_demo, hints=hints)
    diary.save()
    return said


def _step(diary: Diary, text: str, ring: dict | None = None,
          links: list[Link] | None = None, parser: Parser | None = None,
          origin: str = "page", links_from_demo: bool = False,
          hints: list[Link] | None = None) -> list[dict]:
    """
    Один шаг разговора. Дописывает в ленту дневника сказанное человеком и ответ,
    возвращает только новые сообщения.

    links - связи, по которым задаём вопросы и делаем выводы. У бота это связи
    самого человека, у страницы пока придуманный дневник для показа, поэтому
    формулировка вывода немного разная.

    hints - связи с пониженным порогом (`Diary.hints`). Из них получаются
    наблюдения, которые дневник говорит, пока настоящих выводов ещё нет.

    Сетевых вызовов тут два - разбор речи моделью и погода, - и оба делаются до
    того, как берём замок дневника: держать замок во время запроса к сервису
    нельзя, иначе один медленный ответ останавливает и страницу, и бота.
    """
    links = links or []
    parsed = parse_day(text, date.today(), parser)
    # Города нет - вернётся пустота, и погода в разговоре просто не участвует
    sky = weather.readings(diary.city)

    now = date.today()
    with diary.lock:
        # Пропуск считаем до того, как заведём сегодняшнюю запись: иначе
        # сегодняшний день уже стоит в дневнике и «последняя запись» - это он.
        missed = missed_days(diary.timeline, now)

        # Запись за сегодня заводим уже под замком: страница и чат в один
        # момент заводили две записи на один день, и половина фактов уезжала
        # в ту из них, которую потом никто не смотрел.
        record = diary.today()
        weather.attach(diary.timeline, sky)
        # Что из погоды вышло, человек должен услышать, а не только дневник:
        # пока строку никто не говорил, работающий источник выглядел как
        # неработающий - фактор молча ложился в день, и узнать про него было
        # неоткуда до самых выводов.
        sky_note = weather.day_note(sky.get(now), sky.get(now - timedelta(days=1)))
        before_seq = diary.seq
        pending = diary.pending_question(record.day)

        # Рассказ бывает про прошедший день: «вчера пил вино». Такую запись
        # кладём в тот день, а не в сегодняшний. Ответ на висящий вопрос так не
        # толкуем: «на 7, но вчера лёг рано» - это оценка за сегодня.
        spoken = None if pending else day_mentioned(text, now)
        if spoken is not None:
            record = diary.record_for(spoken)

        diary.say("me", text, origin=origin)

        # Реплика бывает ответом и рассказом сразу: «на 3, вымотан после зала» -
        # это и оценка сил, и тренировка. Поэтому применяем оба разбора, а не
        # выбираем один: раньше половина сказанного пропадала, а вопрос уже
        # лежал в asked и второй раз не задавался.
        detail = detail_of(pending) if pending else ""
        answered = bool(pending) and _answers(record, pending, text)
        added = _absorb(record, parsed)

        # Спросили деталь привычки («сколько чашек кофе») - ответ надо принять,
        # иначе разговор выглядит издевательством: бот сам спросил и сам же не
        # понял. Деталь дописываем к факту привычки, чтобы она осталась в дне.
        if detail and not answered and _looks_like_detail(text):
            kept = _remember_detail(record, detail, text)
            if kept is not None:
                diary.forget_question()
                answered = True
                if kept not in added:
                    added = added + [kept]

        if answered:
            # Ответ кладём последним: он должен вытеснить оценку по умолчанию,
            # которую правила поставили тому же показателю («тревога какая-то»).
            apply_answer(record, pending, text)
            diary.forget_question()
            scored = _metric_fact(record, metric_of(pending))
            if scored is not None:
                # В «Записал» идёт итоговая оценка, а не догадка правил
                added = [f for f in added
                         if not (f.kind == "metric" and f.name == scored.name)]
                added.append(scored)
        elif pending and not _polite(text):
            # Не признали ответом - вопрос остаётся висеть, но не вечно. Иначе
            # он пропадал насовсем: спросить второй раз мешает asked, а
            # следующая реплика проверялась уже ни против чего. Реплика «на 7,
            # но это скорее из-за того что вчера лёг рано» так теряла и оценку,
            # и сам вопрос.
            #
            # «Привет» и «спасибо» срок вопроса не тратят: человек поздоровался,
            # а не попытался ответить. Иначе три вежливые реплики подряд гасили
            # вопрос, и настоящий ответ прилетал уже в пустоту.
            diary.wait_longer()

        return _answer(diary, record, text, added, answered, ring, links, origin,
                       links_from_demo, before_seq, hints or [], missed, now,
                       sky_note)


def _answer(diary: Diary, record: DayRecord, text: str, added: list[Fact],
            answered: bool, ring: dict | None, links: list[Link], origin: str,
            links_from_demo: bool, before_seq: int,
            hints: list[Link], missed: list[date], now: date,
            sky_note: str = "") -> list[dict]:
    """
    Что сказать в ответ. Решаем по тому, что дала именно эта реплика: пока
    решение принималось по накопленной за день записи, на «привет» бот заново
    перечислял все привычки с утра и второй раз задавал тот же вопрос.

    Вызывается под замком дневника, сети тут нет.
    """
    # Показания с прибора - всегда про сегодня: на странице это ползунки, они
    # показывают сегодняшнее состояние. Когда человек рассказывает про вчера,
    # запись идёт во вчерашний день, а показания туда попадать не должны -
    # иначе в прошлом дне появляются выдуманные цифры, которых прибор не мерил.
    if ring and record.day == date.today():
        known = list(record.facts)
        readings = SberRingSource().read([{**ring, "date": record.day.isoformat()}])
        merge_into(diary.timeline, readings)
        derive_factors(diary.timeline)
        # Показания идут с каждой репликой, в том числе с «привет». Объявлять их
        # человеку имеет смысл только вместе с тем, что он рассказал сам: иначе
        # на приветствие он получает «Записал: мало спал (с кольца)».
        if added or answered:
            added = added + [f for f in record.facts if f not in known]

    if added:
        # Чем разобрана фраза - техническая деталь, человеку она не нужна.
        # А вот день назвать надо, если запись легла не в сегодняшний: человек
        # должен видеть, что его «вчера» мы поняли именно как вчера.
        when = "" if record.day == now else f" за {day_name(record.day, now)}"
        diary.say("bot", f"Записал{when}:", note=facts_note(added), origin=origin)
    elif not answered:
        # Реплика ничего не добавила и ответом на вопрос не была - «Записал»
        # писать не о чем, отвечаем по-человечески.
        return _small_talk(diary, text, record.day, origin, before_seq)

    # Про пропущенные дни спрашиваем раньше всего остального: вчерашний день
    # человек ещё помнит, а через неделю уже нет. Вопрос один и только один раз
    # за день - иначе возвращение в дневник выглядит как выговор.
    if missed and diary.gap_asked != now:
        asking = gap_question(missed, now)
        if asking:
            diary.gap_asked = now
            diary.say("ask", asking, origin=origin)
            return diary.feed(before_seq)

    # Говорим про то, что человек назвал ЭТОЙ фразой, а не про всё, что успел
    # рассказать за день: иначе он пишет про мясо и пиво, а в ответ слышит про
    # утренний кофе - и разговор выглядит бессвязным.
    habits = frozenset(f.name for f in added if f.kind == "factor" and f.value > 0)

    # Пока связей ещё нет, дневник не должен молчать: сегодняшний день видно на
    # фоне прежних уже на нескольких записях. Это наблюдения, а не выводы, и
    # называем мы их именно так.
    #
    # Наблюдение идёт ДО вопроса, а не вместо него: это одна строка про то, что
    # мы уже увидели, а не отдельная ветка разговора. Пока оно стояло после,
    # любой уточняющий вопрос про названную привычку («сколько пива?») выходил
    # из ответа раньше - и про эту самую привычку человек не слышал ничего.
    # Погода - такое же наблюдение про сегодня, и повторов боится так же:
    # держим её в том же diary.observed. Строка меняется вместе с числами дня,
    # поэтому за день она скажется один раз и назавтра не повторится слово в
    # слово. Обычная погода сюда не доходит вовсе - day_note молчит, пока не
    # сработал хоть один фактор.
    if sky_note and sky_note not in diary.observed:
        diary.observed.add(sky_note)
        diary.say("bot", sky_note, origin=origin)

    seen = observations(diary.timeline, record, habits, hints)
    fresh = [line for line in seen if line not in diary.observed]
    if fresh:
        diary.observed.update(fresh)
        diary.say("bot", "\n".join(fresh), origin=origin)

    # Дошли до предела вопросов за день - дальше работаем с тем, что рассказали.
    if len(diary.asked) >= MAX_QUESTIONS:
        question = None
    else:
        question = next_question(record, links, diary.asked)
    if question:
        if question == diary.pending:
            # Тот же вопрос второй раз подряд - самая быстрая причина бросить
            # дневник. Он и так висит, человек ответит на него, когда захочет.
            return diary.feed(before_seq)
        diary.expect(question, record.day)
        diary.asked.add(question)
        diary.say("ask", question, origin=origin)
        return diary.feed(before_seq)

    if not habits or (diary.concluded is not None and habits <= diary.concluded):
        return _wrap_up(diary, record, origin, before_seq)

    diary.concluded = (diary.concluded or frozenset()) | habits
    said = conclusions(habits, links, len(diary.timeline), links_from_demo)
    if not said:
        return _wrap_up(diary, record, origin, before_seq)

    diary.say("bot", "Картина дня понятна. Смотрю, что об этом говорит ваш дневник.",
              origin=origin)
    for message in said:
        diary.say(message["kind"], message["text"], message.get("note", ""), origin=origin)
    return diary.feed(before_seq)


def _wrap_up(diary: Diary, record: DayRecord, origin: str,
             before_seq: int) -> list[dict]:
    """
    Закрыть день, когда спрашивать больше нечего, а выводов ещё нет.

    Раньше разговор в этом месте просто обрывался: человек отвечал на последний
    вопрос, получал «Записал:» - и тишина. Выглядит как поломка, хотя программа
    просто закончила работу. Говорим, что записали и чего ждём дальше.
    """
    if diary.pending or diary.wrapped_day == record.day:
        return diary.feed(before_seq)       # вопрос ещё висит или итог уже сказан

    habits = [f.name for f in record.facts if f.kind == "factor" and f.value > 0
              and f.source not in DEVICE_SOURCES]
    if not habits:
        return diary.feed(before_seq)

    diary.wrapped_day = record.day
    days = len(diary.timeline)
    listed = ", ".join(dict.fromkeys(habits))
    if days < MIN_DAYS_FOR_HINT:
        left = MIN_DAYS_FOR_HINT - days
        diary.say("bot",
                  f"День записал: {listed}. Пока сравнивать почти не с чем - "
                  f"дней в дневнике {days}. Дней через {left} начну показывать, "
                  f"что с чем сходится. Если ещё что-то вспомните - допишите.",
                  origin=origin)
    else:
        diary.say("bot",
                  f"День записал: {listed}. Ничего нового про эти привычки пока "
                  f"не вижу - продолжаю следить. Если ещё что-то вспомните, "
                  f"допишите, это лишним не будет.",
                  origin=origin)
    return diary.feed(before_seq)


def _small_talk(diary: Diary, text: str, day: date, origin: str,
                before_seq: int) -> list[dict]:
    """Ответ на реплику, в которой мы ничего не разобрали."""
    lowered = text.strip().lower()
    if _GREETING.match(lowered):
        diary.say("bot", GREETING_REPLY, origin=origin)
    elif _THANKS.match(lowered):
        diary.say("bot", THANKS_REPLY, origin=origin)
    elif diary.pending == NO_STATE and _bare_score(lowered):
        # «А как вы себя чувствовали?» - вопрос без показателя, и голая оценка
        # к нему не привязывается: семь чего? Спрашиваем прямо, а не отвечаем
        # «не понял» на осмысленную реплику.
        diary.say("bot", WHICH_METRIC, origin=origin)
    elif diary.pending:
        diary.say("bot", DID_NOT_GET_IT, origin=origin)
    else:
        # Вопросов не ждём - значит, самое время подсказать, как рассказать
        diary.expect(NOTHING_UNDERSTOOD, day)
        diary.say("ask", NOTHING_UNDERSTOOD, origin=origin)
    return diary.feed(before_seq)


def conclusions(habits: frozenset[str] | set[str], links: list[Link], days: int = 0,
                links_from_demo: bool = False) -> list[dict]:
    """
    Что известно про названные привычки. Пустой список - сказать нечего.

    Принимает именно набор привычек, а не запись за день: говорить надо про то,
    что человек назвал сейчас.
    """
    found = [l for l in links if l.factor in habits and l.strength != "наблюдение"]
    if not found:
        if days < MIN_DAYS_FOR_HINT:
            # В начале дневника «выводов пока нет» на каждую фразу звучит как
            # отписка: их и не может быть, пока дней мало. Молчим.
            return []
        return [{"kind": "bot", "note": f"Дней в дневнике: {days}.",
                 "text": "Про эти привычки выводов пока нет: нужно хотя бы семь дней "
                         "с привычкой и семь дней без неё. Продолжайте записывать."}]

    head = ("Вот что про эти привычки говорит придуманный дневник за несколько месяцев - "
            "на ваших записях выводы появятся так же, недели через три."
            if links_from_demo else
            "Вот что про эти привычки говорит ваш дневник.")
    return ([{"kind": "bot", "text": head}]
            + [{"kind": "result", "text": say(l), "note": f"{basis(l)} {next_step(l)}"}
               for l in found])


def _absorb(record: DayRecord, parsed: DayRecord | None) -> list[Fact]:
    """
    Дописать разобранное в запись дня и вернуть то, что правда добавилось.

    Именно добавившееся мы потом и показываем: про кофе, названный час назад,
    второй раз говорить «записал» не нужно.
    """
    added = []
    for fact in (parsed.facts if parsed else []):
        known = list(record.facts)
        record.add(fact)             # тот же показатель за день не удваивается
        if fact in record.facts and fact not in known:
            added.append(fact)
    return added


def _metric_fact(record: DayRecord, metric: str) -> Fact | None:
    """Факт-показатель за день целиком, вместе с оценкой - для показа человеку."""
    return next((f for f in record.facts if f.kind == "metric" and f.name == metric), None)


def _answers(record: DayRecord, question: str, text: str) -> bool:
    """
    Есть ли в реплике оценка по заданному вопросу. Спрашиваем ровно тот же
    разбор, который потом впишет ответ в дневник.

    Ответ на вопрос со шкалой - это оценка: «на 7», «часов шесть», «нормально».
    Наличие рассказа рядом с оценкой ей не мешает - «на 3, вымотан после зала»
    это и оценка сил, и тренировка. А вот число при мере оценкой не бывает:
    «спал 5 часов» в ответ про тревогу давало «тревога 5», «сегодня 8 встреч» -
    «тревога 8». Вопрос про сон - исключение: часы там и есть ответ.

    Развёрнутый рассказ ответом не считаем вовсе: в нём число почти наверняка
    про что-то своё, а оценку человек ещё успеет назвать - вопрос останется.

    Раньше ответ искали по разнице значений в записи, и совпадение с догадкой
    правил читалось как молчание: на «тревога какая-то» правила ставят 3.0, и
    честное «3» от человека вопрос не закрывало.
    """
    if len(text.split()) > MAX_ANSWER_WORDS:
        return False

    if score_in(question, text, record.day) is None:
        return False        # оценки в реплике не нашлось

    return metric_of(question) == "качество сна" or not measured_number(text)
