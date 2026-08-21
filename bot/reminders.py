"""
Напоминания записать день.

Дневники бросают не потому, что они плохие, а потому что про них забывают:
день прошёл, вечером не до записей, назавтра неловко возвращаться. Поэтому
напоминание - не украшение продукта, а часть его работы.

Тут только расписание: кому, во сколько и не писали ли уже сегодня. Отправку
делает бот - здесь нет ни сети, ни Telegram, и поэтому всё это можно проверить
тестом, не поднимая ни того ни другого.

Всё в памяти процесса, как и остальной прототип: перезапустили - расписание
собирается заново из тех, кто напишет боту. База появится вместе с личным
кабинетом.
"""
from __future__ import annotations

import re
import threading
from datetime import date, datetime

# Вечер: день уже прожит и его есть чем описать, но человек ещё не спит.
DEFAULT_TIME = "21:00"

# С этого часа уже молчим: программу могли не запускать весь вечер, и тогда
# «пора напоминать» наступает разом в полночь. Ночью это не помощь, а помеха -
# лучше промолчать и напомнить завтра.
LATEST_HOUR = 23

TEXT = ("Как прошёл день? Пара фраз - и я запишу: что делали и как себя "
        "чувствовали. Не до этого сегодня - просто не отвечайте.")

_TIME_SHAPE = re.compile(r"^(\d{1,2})(?:[:.\s](\d{2}))?$")

# Сколько чатов держим в расписании. Боту пишет кто угодно, а память не
# резиновая - тот же предел, что у непривязанных дневников в `wam.diary`.
MAX_CHATS = 200


def minutes_of(text: str) -> int:
    """
    Время из строки в минутах от полуночи: «21», «21:00», «9.30».
    Кривое время - ValueError: молча подставить своё значит обмануть.
    """
    match = _TIME_SHAPE.match((text or "").strip())
    if not match:
        raise ValueError("время пишется так: 21:00")
    hours = int(match.group(1))
    minutes = int(match.group(2) or 0)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError("время пишется так: 21:00")
    return hours * 60 + minutes


def as_clock(minutes: int) -> str:
    """Минуты от полуночи обратно в «21:00» - так время и показываем."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class Schedule:
    """
    Кому и когда напоминать.

    Замок нужен: расписание правит поток, который разбирает сообщения, а
    читает его тот же поток между опросами Telegram - но с командой «Отключить»
    со страницы потоков становится два.
    """

    def __init__(self, at: str = DEFAULT_TIME) -> None:
        self._lock = threading.Lock()
        self._default = minutes_of(at)
        self._at: dict[int, int] = {}       # чат -> когда напоминать
        # Выключенные держим отдельно от времени: человек выключил напоминания
        # на день-другой, а включив обратно, ждёт своё прежнее время, а не
        # общее вечернее.
        self._off: set[int] = set()
        self._sent: dict[int, date] = {}
        self._chats: list[int] = []

    def remember(self, chat_id: int) -> None:
        """Чат, который написал боту: только таким и есть смысл напоминать."""
        with self._lock:
            if chat_id in self._chats:
                return
            self._chats.append(chat_id)
            if len(self._chats) > MAX_CHATS:
                # Выбывает тот, кто написал раньше всех: его дневник в памяти
                # тоже уже мог истечь.
                gone = self._chats.pop(0)
                self._at.pop(gone, None)
                self._off.discard(gone)
                self._sent.pop(gone, None)

    def chats(self) -> list[int]:
        with self._lock:
            return list(self._chats)

    def set_time(self, chat_id: int, text: str) -> str:
        """Поменять время. Возвращает его же в виде «21:00»."""
        at = minutes_of(text)
        self.remember(chat_id)      # до записи: иначе чат выпадет из обхода
        with self._lock:
            self._at[chat_id] = at
            self._off.discard(chat_id)      # назначил время - значит, они нужны
        return as_clock(at)

    def turn_off(self, chat_id: int) -> None:
        with self._lock:
            self._off.add(chat_id)

    def turn_on(self, chat_id: int) -> str:
        """Вернуть напоминания на прежнее время; не задавали - на вечернее."""
        self.remember(chat_id)
        with self._lock:
            self._off.discard(chat_id)
            return as_clock(self._at.get(chat_id, self._default))

    def is_on(self, chat_id: int) -> bool:
        with self._lock:
            return chat_id not in self._off

    def time_of(self, chat_id: int) -> str:
        """Время напоминания словами; пустая строка - напоминания выключены."""
        with self._lock:
            if chat_id in self._off:
                return ""
            return as_clock(self._at.get(chat_id, self._default))

    def due(self, chat_id: int, now: datetime) -> bool:
        """
        Пора ли напоминать этому чату. Про то, писал ли человек сегодня, тут
        не знают - это спрашивает бот у дневника, потому что писать человек мог
        и со страницы.
        """
        with self._lock:
            if chat_id in self._off:
                return False
            at = self._at.get(chat_id, self._default)
            if self._sent.get(chat_id) == now.date():
                return False        # сегодня уже напоминали, второго раза не будет
        if now.hour >= LATEST_HOUR:
            return False
        return now.hour * 60 + now.minute >= at

    def mark_sent(self, chat_id: int, day: date) -> None:
        """
        Отметить день как закрытый - и когда напомнили, и когда не стали.
        Иначе тот, кто пишет каждый вечер, проверялся бы снова каждые полминуты.
        """
        with self._lock:
            self._sent[chat_id] = day
