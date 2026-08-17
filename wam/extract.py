"""
Превращение свободного рассказа в факты.

В продукте это делает языковая модель: человек говорит голосом, модель
достаёт события, факторы и оценку состояния. Здесь реализованы два пути:

* `RuleExtractor` — разбор по правилам, работает без ключей и без сети.
  Он же страхует продукт, когда модель недоступна: процесс не должен
  останавливаться из-за внешнего сервиса.
* `LLMExtractor` — интерфейс к модели. Промпт и разбор ответа здесь же,
  сам вызов вынесен в `llm.py`, чтобы провайдера можно было заменить.

Ключевая деталь — нормализация: «не выспался», «спал 4 часа» и «почти
не спал» должны стать одним фактором, иначе связи никогда не наберут
статистики.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Protocol

from .schema import DayRecord, Fact

# ── Словарь нормализации ──────────────────────────────────────────────────
# Ключ — то, как человек говорит; значение — канонический факт.
_FACTOR_PATTERNS: list[tuple[str, str]] = [
    (r"коф[еия]|эспрессо|капучино|латте",           "кофе"),
    (r"алкогол|вин[оа]|пив[оа]|виски|коктейл",       "алкоголь"),
    (r"трениров|зал|бег|пробеж|йог|плаван",          "тренировка"),
    (r"перелёт|перелет|самол[её]т|рейс",             "перелёт"),
    (r"совещан|созвон|встреч[аи]|переговор",         "много встреч"),
    (r"допоздна|поздно лёг|поздно лег|за полноч",    "поздний отбой"),
    (r"экран|сериал|листал|скролл",                  "экран перед сном"),
    (r"прогул|гулял|на улице|пешком",                "прогулка"),
    (r"сахар|сладк|десерт|торт|пирожн",              "сладкое"),
    (r"дедлайн|аврал|завал|горит",                   "аврал",),
]

_METRIC_PATTERNS: list[tuple[str, str]] = [
    (r"тревог|беспоко|на нервах|паник",              "тревога"),
    (r"голов[ан].*болит|мигрен|болит голова",        "головная боль"),
    (r"выспал|сон|спал|засыпа",                      "качество сна"),
    (r"энерг|сил[ыа]|бодр|устал|разбит",             "энергия"),
    (r"настроен|раздраж|злюсь|радост",               "настроение"),
]

# Оценки состояния, если человек называет их словами, а не цифрой
_WORD_SCORES: list[tuple[str, float]] = [
    (r"ужасн|отврат|хуже некуда|разбит",  2.0),
    (r"плох|так себе|слаб|устал",         4.0),
    (r"норм|обычн|ровн",                  6.0),
    (r"хорош|бодр|лучше",                 8.0),
    (r"отличн|прекрасн|супер|отлично",    9.0),
]

_NEGATION = re.compile(r"\b(не|без|нет)\s+\w*", re.IGNORECASE)

# Люди говорят «спал часов пять», а не «спал 5 часов»
_WORD_NUMBERS = {
    "один": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}

# Если человек просто назвал плохое состояние и не поставил оценку —
# считаем это низким баллом. Молчать в такой ситуации хуже, чем округлить:
# «тревога какая-то» это точно не десятка.
_NEGATIVE_BY_DEFAULT = {"тревога": 3.0, "головная боль": 3.0}


class Extractor(Protocol):
    """Любой разборщик рассказа — правила или модель."""

    def extract(self, text: str, day: date) -> DayRecord: ...


class RuleExtractor:
    """Разбор по правилам: быстрый, бесплатный, полностью офлайн."""

    def extract(self, text: str, day: date) -> DayRecord:
        record = DayRecord(day=day, raw_text=text)
        lowered = text.lower()

        for pattern, name in _FACTOR_PATTERNS:
            match = re.search(pattern, lowered)
            if not match:
                continue
            # «не пил кофе» — это факт об отсутствии фактора, а не о его наличии
            window = lowered[max(0, match.start() - 20):match.start()]
            value = 0.0 if _NEGATION.search(window) else 1.0
            record.add(Fact("factor", name, value, "diary", quote=_around(text, match.start())))

        for pattern, name in _METRIC_PATTERNS:
            match = re.search(pattern, lowered)
            if not match:
                continue
            score = _score_near(lowered, match.start(), metric=name)
            if score is None:
                score = _NEGATIVE_BY_DEFAULT.get(name)
            if score is not None:
                record.add(Fact("metric", name, score, "diary", quote=_around(text, match.start())))

        return record


class LLMExtractor:
    """
    Разбор моделью. Модель отвечает строгим JSON — свободный текст в ответе
    не принимается, иначе факты невозможно свести к общим именам.
    """

    PROMPT = (
        "Ты разбираешь дневниковую запись человека. Верни СТРОГО JSON вида\n"
        '{"factors": [{"name": "...", "value": 1}], '
        '"metrics": [{"name": "...", "value": 0..10}], "events": ["..."]}\n'
        "Правила:\n"
        "1. name — короткое каноническое имя в именительном падеже: «кофе», «тренировка», «перелёт».\n"
        "2. Одинаковые по смыслу формулировки своди к одному имени.\n"
        "3. metrics — только оценки состояния по шкале 0..10 (сон, тревога, энергия, настроение, боль).\n"
        "4. Ничего не выдумывай: если про фактор не сказано, не добавляй его.\n"
        "Запись:\n"
    )

    def __init__(self, complete) -> None:
        """complete(prompt: str) -> str — любой вызов модели, возвращающий текст."""
        self._complete = complete

    def extract(self, text: str, day: date) -> DayRecord:
        record = DayRecord(day=day, raw_text=text)
        raw = self._complete(self.PROMPT + text)
        data = _parse_json(raw)
        for item in data.get("factors", []):
            record.add(Fact("factor", str(item.get("name", "")).strip().lower(),
                            float(item.get("value", 1)), "diary"))
        for item in data.get("metrics", []):
            record.add(Fact("metric", str(item.get("name", "")).strip().lower(),
                            float(item.get("value", 0)), "diary"))
        for name in data.get("events", []):
            record.add(Fact("event", str(name).strip().lower(), 1.0, "diary"))
        return record


def _around(text: str, index: int, width: int = 40) -> str:
    start = max(0, index - width // 2)
    return text[start:start + width].strip()


def _score_near(lowered: str, index: int, window: int = 60, metric: str = "") -> float | None:
    """Оценка состояния рядом с упоминанием: цифрой («сон на 3») или словом."""
    fragment = lowered[max(0, index - window):index + window]

    digits = re.search(r"\b(10|[0-9])\s*(?:/\s*10|из\s*10|балл)", fragment)
    if digits:
        return float(digits.group(1))

    # «спал 5 часов», «часов пять поспал» — это мера сна и только его:
    # рядом стоящая «тревога» не должна получить оценку из часов
    if metric != "качество сна":
        return _word_score(fragment)

    hours = re.search(r"(\d{1,2})\s*час", fragment) or re.search(r"час\w*\s+(\d{1,2})\b", fragment)
    if hours:
        return min(10.0, round(float(hours.group(1)) / 8 * 10, 1))

    # Рядом со словом «час» может оказаться и глагол («спал часов»), поэтому
    # смотрим слова и справа, и слева, и берём первое, которое правда число.
    for pattern in (r"час\w*\s+(\w+)", r"(\w+)\s+час"):
        for match in re.finditer(pattern, fragment):
            number = _WORD_NUMBERS.get(match.group(1))
            if number:
                return min(10.0, round(number / 8 * 10, 1))

    return _word_score(fragment)


def _word_score(fragment: str) -> float | None:
    """Оценка словами: «ужасно», «нормально», «отлично»."""
    for pattern, score in _WORD_SCORES:
        if re.search(pattern, fragment):
            return score
    return None


def _parse_json(raw: str) -> dict:
    """Модель иногда оборачивает JSON в пояснения — достаём первый объект."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
