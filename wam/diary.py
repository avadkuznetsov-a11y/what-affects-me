"""
Общий дневник: одно место, где лежат записи, кто бы их ни присылал.

У страницы и у бота нет ничего общего, кроме этого хранилища. Человек написал
что-то в Telegram - факты попали в тот же дневник, который открыт на странице,
и наоборот. Поэтому запись живёт не внутри веб-сессии и не внутри бота, а здесь.

Всё в памяти процесса: перезапустили - записи начинаются заново. Для прототипа
этого достаточно, база появится вместе с личным кабинетом.

Про замки. У хранилища один замок на всю раскладку дневников и коды, у каждого
дневника свой - на один шаг разговора. Правило простое и его нельзя нарушать:
под замком не делаем сетевых вызовов. Ни запроса к модели, ни отправки в
Telegram - иначе один медленный ответ сервиса останавливает всех остальных.
"""
from __future__ import annotations

import random
import re
import threading
import time
from datetime import date

from .derive import derive_factors
from .insights import Link, find_links
from .schema import DayRecord, Timeline

# Код привязки живёт недолго: это пароль к дневнику, произнесённый вслух.
CODE_LIFETIME = 15 * 60
CODE_TAIL = "МИРА"

# Лента в памяти не растёт бесконечно: старые сообщения из показа уходят.
MAX_MESSAGES = 400

_secrets = random.SystemRandom()


class Diary:
    """
    Один дневник: записи по дням, вопрос, на который ждём ответа, и лента
    сообщений для показа на странице.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.timeline = Timeline()
        self.pending: str | None = None      # заданный вопрос, ждём на него ответ
        self.asked: set[str] = set()         # чтобы не спрашивать одно и то же
        self.messages: list[dict] = []
        self.seq = 0                         # номер последнего сообщения в ленте
        self.lock = threading.Lock()         # один шаг разговора за раз
        self._links: list[Link] = []
        self._links_version = -1

    # ── записи ────────────────────────────────────────────────────────────
    def today(self) -> DayRecord:
        """Запись за сегодня. Если её ещё нет - появится."""
        day = date.today()
        for record in self.timeline.days:
            if record.day == day:
                return record
        record = DayRecord(day=day)
        self.timeline.add(record)
        return record

    def add(self, record: DayRecord) -> None:
        """Добавить готовую запись; запись за тот же день дополняет прежнюю."""
        for existing in self.timeline.days:
            if existing.day == record.day:
                for fact in record.facts:
                    existing.add(fact)
                return
        self.timeline.add(record)

    # ── связи ─────────────────────────────────────────────────────────────
    def links(self) -> list[Link]:
        """
        Связи в этом дневнике. Считать их дорого, а за одно сообщение они
        нужны несколько раз, поэтому держим посчитанное до следующей записи.
        Версия дневника - это просто число фактов в нём: так кэш не устареет
        из-за того, что кто-то забыл про него сказать.
        """
        if self._version() != self._links_version:
            self._links = find_links(derive_factors(self.timeline))
            # derive_factors дописывает факторы с прибора, фактов после этого
            # больше - запоминаем именно итоговое число
            self._links_version = self._version()
        return self._links

    def _version(self) -> int:
        return sum(len(record.facts) for record in self.timeline.days)

    # ── лента сообщений ───────────────────────────────────────────────────
    def say(self, kind: str, text: str, note: str = "", origin: str = "page") -> dict:
        """Положить сообщение в ленту. origin - откуда пришло: page или chat."""
        self.seq += 1
        message = {"kind": kind, "text": text, "note": note,
                   "from": origin, "seq": self.seq}
        self.messages.append(message)
        if len(self.messages) > MAX_MESSAGES:
            del self.messages[:-MAX_MESSAGES]
        return message

    def feed(self, since: int = 0) -> list[dict]:
        """Сообщения, которых у страницы ещё нет."""
        return [m for m in self.messages if m["seq"] > since]

    def reset(self) -> None:
        """
        Начать разговор заново: лента и сегодняшняя запись чистые. Прошлые дни
        остаются - в них весь смысл дневника, в том числе то, что пришло из чата.
        """
        self.timeline.days = [d for d in self.timeline.days if d.day != date.today()]
        self.pending = None
        self.asked = set()
        self.messages = []
        self._links_version = -1


class DiaryStore:
    """
    Все дневники процесса и привязка чатов Telegram к ним.

    Ключ дневника - строка: у страницы это "web", у непривязанного чата
    "tg:<chat_id>". Код привязки одноразовый и живёт четверть часа.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._diaries: dict[str, Diary] = {}
        self._codes: dict[str, tuple[str, float]] = {}     # код -> (ключ, когда выдан)
        self._chats: dict[int, str] = {}                   # чат -> ключ дневника

    def get(self, key: str) -> Diary:
        """Дневник по ключу; один ключ - всегда один и тот же дневник."""
        with self._lock:
            diary = self._diaries.get(key)
            if diary is None:
                diary = Diary(key)
                self._diaries[key] = diary
            return diary

    # ── привязка Telegram ─────────────────────────────────────────────────
    def new_code(self, key: str) -> str:
        """Новый код привязки для дневника. Прежний код этого дневника гаснет."""
        with self._lock:
            self._forget_stale()
            self._codes = {code: value for code, value in self._codes.items()
                           if value[0] != key}
            while True:
                code = f"{_secrets.randrange(1000, 10000)}-{CODE_TAIL}"
                if code not in self._codes:
                    break
            self._codes[code] = (key, time.time())
            return code

    def bind(self, code: str, chat_id: int) -> str | None:
        """
        Привязать чат к дневнику по коду. Возвращает ключ дневника или None,
        если код чужой, уже использованный или просроченный.
        """
        with self._lock:
            self._forget_stale()
            found = self._codes.pop(self._normalise(code), None)
            if found is None:
                return None
            key = found[0]
            self._chats[chat_id] = key
            return key

    def key_for_chat(self, chat_id: int) -> str:
        """
        Ключ дневника для чата. Непривязанный чат получает свой собственный
        дневник: бот полезен и сам по себе, без открытой страницы.
        """
        with self._lock:
            return self._chats.get(chat_id) or f"tg:{chat_id}"

    def diary_for_chat(self, chat_id: int) -> Diary:
        return self.get(self.key_for_chat(chat_id))

    def linked_chats(self, key: str) -> list[int]:
        """Чаты, привязанные к этому дневнику."""
        with self._lock:
            return [chat for chat, value in self._chats.items() if value == key]

    def _forget_stale(self) -> None:
        deadline = time.time() - CODE_LIFETIME
        self._codes = {code: value for code, value in self._codes.items()
                       if value[1] > deadline}

    @staticmethod
    def _normalise(code: str) -> str:
        """Человек перепишет код как получится: с пробелами, строчными, без тире."""
        text = "".join(str(code).split()).upper().replace("—", "-").replace("–", "-")
        return text


_CODE_SHAPE = re.compile(r"\d{4}\s*[-—–]?\s*" + CODE_TAIL)


def looks_like_code(text: str) -> bool:
    """
    Похоже ли сообщение на код привязки. Проверять надо до разбора речи:
    в «4821-МИРА» разборщик увидит ноль фактов и бот начнёт спрашивать
    про самочувствие вместо того, чтобы привязать чат.
    """
    return bool(_CODE_SHAPE.fullmatch(text.strip().upper()))
