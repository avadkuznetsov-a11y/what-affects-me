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

# Азбука кода: без нуля и буквы O, без единицы и букв I и L - их путают на
# слух и в переписке. Восемь знаков из тридцати одного - это сорок бит: столько
# наугад не подберёшь, а прежние четыре цифры подбирались за вечер.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 8

# Сколько раз подряд можно ошибиться кодом. Считаем и по чату, и по всем сразу:
# один упрямый чат уходит в тишину, а толпа чатов гасит сам код.
CODE_TRIES_PER_CHAT = 5
CODE_PAUSE = 60
CODE_TRIES_TOTAL = 20

# Непривязанные дневники заводятся на любого, кто написал боту, поэтому их
# число и срок жизни ограничены: иначе память занимает первый же случайный чат.
GUEST_DIARIES = 200
GUEST_LIFETIME = 7 * 24 * 60 * 60

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
        # Один шаг разговора за раз. Замок повторный: его берут и сами методы
        # дневника, и код снаружи - на обычном вложенный вызов повесил бы поток.
        self.lock = threading.RLock()
        self._links: list[Link] = []
        self._links_version: int | None = None

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

        Версия дневника - слепок содержимого фактов, а не их число. По числу
        кэш протухал молча: ответ «на 7» заменяет прежнюю оценку тревоги, факт
        уходит и приходит, счётчик прежний - и мы отдавали старые связи.
        """
        with self.lock:
            if self._version() != self._links_version:
                self._links = find_links(derive_factors(self.timeline))
                # derive_factors дописывает факторы с прибора, фактов после
                # этого больше - запоминаем именно итоговый слепок
                self._links_version = self._version()
            return self._links

    def _version(self) -> int:
        return hash(tuple((record.day, fact.kind, fact.name, fact.value, fact.source)
                          for record in self.timeline.days for fact in record.facts))

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

        Замок берём сами: чистка ленты посреди чужого шага разговора теряет
        сообщения, а страница зовёт сброс кнопкой в любой момент.
        """
        with self.lock:
            self.timeline.days = [d for d in self.timeline.days if d.day != date.today()]
            self.pending = None
            self.asked = set()
            self.messages = []
            self._links_version = None


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
        self._misses: dict[int, tuple[int, float]] = {}    # чат -> (промахи, до когда молчим)
        self._misses_total = 0
        self._guests: dict[str, float] = {}                # дневник чата -> когда о нём вспоминали

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
                code = self._draw_code()
                if code not in self._codes:
                    break
            self._codes[code] = (key, time.time())
            self._misses_total = 0      # новый код - счёт промахов с начала
            return code

    def bind(self, code: str, chat_id: int) -> str | None:
        """
        Привязать чат к дневнику по коду. Возвращает ключ дневника или None,
        если код чужой, уже использованный, просроченный или чат сейчас в
        паузе за перебор.

        Всё, что человек уже написал боту, переезжает в дневник страницы:
        иначе привязка выглядит как потеря записей.
        """
        with self._lock:
            if self.code_paused(chat_id):
                return None
            self._forget_stale()
            found = self._codes.pop(self._normalise(code), None)
            if found is None:
                self._count_miss(chat_id)
                return None
            key = found[0]
            self._misses.pop(chat_id, None)
            self._misses_total = 0
            self._move_days(self._chats.get(chat_id) or f"tg:{chat_id}", key)
            self._chats[chat_id] = key
            return key

    def code_paused(self, chat_id: int) -> bool:
        """Не отвечаем ли мы сейчас этому чату на коды: он их подбирает."""
        with self._lock:
            misses, until = self._misses.get(chat_id, (0, 0.0))
            if until and until <= time.time():
                self._misses.pop(chat_id, None)      # пауза вышла, счёт с начала
                return False
            return bool(until)

    def unlink(self, key: str) -> list[int]:
        """Отвязать от дневника все чаты. Возвращает те, что были привязаны."""
        with self._lock:
            chats = self.linked_chats(key)
            for chat in chats:
                del self._chats[chat]
            self._codes = {code: value for code, value in self._codes.items()
                           if value[0] != key}
            return chats

    def key_for_chat(self, chat_id: int) -> str:
        """
        Ключ дневника для чата. Непривязанный чат получает свой собственный
        дневник: бот полезен и сам по себе, без открытой страницы.
        """
        with self._lock:
            key = self._chats.get(chat_id)
            if key:
                return key
            key = f"tg:{chat_id}"
            self._guests[key] = time.time()
            self._forget_guests()
            return key

    def diary_for_chat(self, chat_id: int) -> Diary:
        return self.get(self.key_for_chat(chat_id))

    def linked_chats(self, key: str) -> list[int]:
        """Чаты, привязанные к этому дневнику."""
        with self._lock:
            return [chat for chat, value in self._chats.items() if value == key]

    def _count_miss(self, chat_id: int) -> None:
        """Промах по коду: с одного чата уводит в паузу, со всех - гасит код."""
        misses = self._misses.get(chat_id, (0, 0.0))[0] + 1
        until = time.time() + CODE_PAUSE if misses >= CODE_TRIES_PER_CHAT else 0.0
        self._misses[chat_id] = (misses, until)
        self._misses_total += 1
        if self._misses_total >= CODE_TRIES_TOTAL:
            # Код подбирают из многих чатов - он больше не тайна, гасим его.
            # Человек нажмёт «Показать новый код», это дешевле подобранной привязки.
            self._codes = {}
            self._misses_total = 0

    def _move_days(self, from_key: str, to_key: str) -> None:
        """Перелить записи из прежнего дневника чата в дневник страницы."""
        if from_key == to_key:
            return
        old = self._diaries.pop(from_key, None)
        self._guests.pop(from_key, None)
        if old is None:
            return
        target = self.get(to_key)
        with target.lock, old.lock:
            for record in old.timeline.days:
                target.add(record)

    def _forget_guests(self) -> None:
        """Дневники непривязанных чатов: старые и лишние выкидываем."""
        deadline = time.time() - GUEST_LIFETIME
        drop = {key for key, seen in self._guests.items() if seen < deadline}
        if len(self._guests) - len(drop) > GUEST_DIARIES:
            # Лишние - те, в которые дольше всех не писали
            alive = sorted((key for key in self._guests if key not in drop),
                           key=lambda key: self._guests[key])
            drop.update(alive[:len(alive) - GUEST_DIARIES])
        for key in drop:
            self._guests.pop(key, None)
            self._diaries.pop(key, None)

    def _forget_stale(self) -> None:
        deadline = time.time() - CODE_LIFETIME
        self._codes = {code: value for code, value in self._codes.items()
                       if value[1] > deadline}

    @staticmethod
    def _draw_code() -> str:
        letters = "".join(_secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        return f"{letters[:CODE_LENGTH // 2]}-{letters[CODE_LENGTH // 2:]}"

    @staticmethod
    def _normalise(code: str) -> str:
        """
        Человек перепишет код как получится: строчными, с пробелами, без тире
        или с длинным тире. Оставляем только знаки азбуки и ставим тире сами.
        """
        letters = [c for c in str(code).upper() if c in CODE_ALPHABET]
        if len(letters) != CODE_LENGTH:
            return ""
        return (f"{''.join(letters[:CODE_LENGTH // 2])}-"
                f"{''.join(letters[CODE_LENGTH // 2:])}")


_CODE_SHAPE = re.compile(f"[{CODE_ALPHABET}]{{{CODE_LENGTH // 2}}}"
                         r"\s*[-—–]?\s*"
                         f"[{CODE_ALPHABET}]{{{CODE_LENGTH // 2}}}")


def looks_like_code(text: str) -> bool:
    """
    Похоже ли сообщение на код привязки. Проверять надо до разбора речи:
    в «7QK4-M2XB» разборщик увидит ноль фактов и бот начнёт спрашивать
    про самочувствие вместо того, чтобы привязать чат.
    """
    return bool(_CODE_SHAPE.fullmatch(text.strip().upper()))
