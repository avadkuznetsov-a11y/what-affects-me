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

from datetime import date

from .derive import DEVICE_SOURCES, derive_factors
from .diary import Diary
from .extract import LLMExtractor, RuleExtractor
from .insights import Link
from .llm import available_engine
from .phrases import basis, next_step, say
from .questions import apply_answer, next_question
from .schema import DayRecord
from .wearables import SberRingSource, merge_into

# Больше двух уточнений за разговор - это уже анкета, из-за которых дневники
# и бросают. Дальше работаем с тем, что рассказали.
MAX_QUESTIONS = 2


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
        if self.model is not None:
            try:
                record = self.model.extract(text, day)
                if record.facts:
                    return record
            except Exception:
                pass
        return self.rules.extract(text, day)


_DEFAULT: Parser | None = None


def default_parser() -> Parser:
    """Разборщик по умолчанию. Собирается при первом обращении, не при импорте."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Parser.from_environment()
    return _DEFAULT


def parse_day(text: str, day: date, parser: Parser | None = None) -> DayRecord:
    return (parser or default_parser()).parse(text, day)


def facts_note(record: DayRecord) -> str:
    """
    Показываем только то, что было, и сами показатели. Строки вида
    «много двигался: не было» человеку не нужны - это шум.
    """
    habits, metrics = [], []
    for fact in record.facts:
        if fact.kind == "event":
            continue        # события в поиске связей не участвуют, показывать их незачем
        mark = " (с кольца)" if fact.source in DEVICE_SOURCES else ""
        if fact.kind == "factor":
            if fact.value > 0:
                habits.append(f"{fact.name}{mark}")
            elif fact.source not in DEVICE_SOURCES:
                habits.append(f"{fact.name}: не было")
        else:
            metrics.append(f"{fact.name} {fact.value:g} из 10{mark}")

    lines = []
    if habits:
        lines.append("Привычки: " + ", ".join(dict.fromkeys(habits)))
    if metrics:
        lines.append("Самочувствие: " + ", ".join(dict.fromkeys(metrics)))
    return "\n".join(lines)


def step(diary: Diary, text: str, ring: dict | None = None,
         links: list[Link] | None = None, parser: Parser | None = None,
         origin: str = "page", links_from_demo: bool = False) -> list[dict]:
    """
    Один шаг разговора. Дописывает в ленту дневника сказанное человеком и ответ,
    возвращает только новые сообщения.

    links - связи, по которым задаём вопросы и делаем выводы. У бота это связи
    самого человека, у страницы пока придуманный дневник для показа, поэтому
    формулировка вывода немного разная.

    Сетевой вызов тут один - разбор речи моделью, и он делается до того, как
    берём замок дневника: держать замок во время запроса к сервису нельзя,
    иначе один медленный ответ останавливает и страницу, и бота.
    """
    links = links or []
    pending = diary.pending

    # Короткий ответ на заданный вопрос («на 7», «часов шесть») разбирается
    # правилами, модель для этого не нужна.
    answered = bool(pending) and _answers(diary.today(), pending, text)
    parsed = None if answered else parse_day(text, date.today(), parser)

    with diary.lock:
        record = diary.today()
        before_seq = diary.seq
        diary.say("me", text, origin=origin)

        if answered:
            apply_answer(record, pending, text)
        else:
            _absorb(record, parsed)
        diary.pending = None

        return _answer(diary, record, ring, links, origin, links_from_demo, before_seq)


def _answer(diary: Diary, record: DayRecord, ring: dict | None, links: list[Link],
            origin: str, links_from_demo: bool, before_seq: int) -> list[dict]:
    """Что сказать в ответ. Вызывается под замком дневника, сети тут нет."""
    if ring:
        readings = SberRingSource().read([{**ring, "date": record.day.isoformat()}])
        merge_into(diary.timeline, readings)
        derive_factors(diary.timeline)

    if not record.facts:
        diary.pending = next_question(record, links, diary.asked)
        diary.say("ask", diary.pending, origin=origin)
        return diary.feed(before_seq)

    # Чем разобрана фраза - техническая деталь, человеку она не нужна
    diary.say("bot", "Записал:", note=facts_note(record), origin=origin)

    links_for_question = links if len(diary.asked) < MAX_QUESTIONS else []
    question = next_question(record, links_for_question, diary.asked)
    if question:
        diary.pending = question
        diary.asked.add(question)
        diary.say("ask", question, origin=origin)
        return diary.feed(before_seq)

    diary.say("bot", "Картина дня понятна. Смотрю, что об этом говорит ваш дневник.",
              origin=origin)
    for message in conclusions(record, links, len(diary.timeline), links_from_demo):
        diary.say(message["kind"], message["text"], message.get("note", ""), origin=origin)
    return diary.feed(before_seq)


def conclusions(record: DayRecord, links: list[Link], days: int = 0,
                links_from_demo: bool = False) -> list[dict]:
    """Что известно про названные сегодня привычки."""
    mentioned = {f.name for f in record.facts if f.kind == "factor" and f.value > 0}
    found = [l for l in links if l.factor in mentioned and l.strength != "наблюдение"]
    if not found:
        note = "" if links_from_demo else f"Дней в дневнике: {days}."
        return [{"kind": "bot", "note": note,
                 "text": "Про эти привычки выводов пока нет: нужно хотя бы семь дней "
                         "с привычкой и семь дней без неё. Продолжайте записывать."}]

    head = ("Вот что про эти привычки говорит придуманный дневник за несколько месяцев - "
            "на ваших записях выводы появятся так же, недели через три."
            if links_from_demo else
            "Вот что про эти привычки говорит ваш дневник.")
    return ([{"kind": "bot", "text": head}]
            + [{"kind": "result", "text": say(l), "note": f"{basis(l)} {next_step(l)}"}
               for l in found])


def _absorb(record: DayRecord, parsed: DayRecord | None) -> None:
    for fact in (parsed.facts if parsed else []):
        record.add(fact)


def _answers(record: DayRecord, question: str, text: str) -> bool:
    """
    Понятен ли текст как ответ на заданный вопрос. Проверяем на копии записи,
    чтобы решить это до замка и без обращения к модели.
    """
    probe = DayRecord(day=record.day, facts=list(record.facts))
    apply_answer(probe, question, text)
    return _metrics(probe) != _metrics(record)


def _metrics(record: DayRecord) -> dict:
    return {f.name: f.value for f in record.facts if f.kind == "metric"}
