"""
Общий дневник: одно место, где лежат записи, кто бы их ни присылал.

У страницы и у бота нет ничего общего, кроме этого хранилища. Человек написал
что-то в Telegram - факты попали в тот же дневник, который открыт на странице,
и наоборот. Поэтому запись живёт не внутри веб-сессии и не внутри бота, а здесь.

Записи и привязка чатов переживают перезапуск: их держит `wam/storage.py`.
Разговор - лента сообщений, заданный вопрос, коды привязки - живёт только в
памяти процесса и после перезапуска начинается заново. Хранилище без файла
(`DiaryStore()` без пути) работает как раньше, целиком в памяти: так устроены
тесты, чтобы они не писали в настоящий дневник человека.

Про замки. У хранилища один замок на всю раскладку дневников и коды, у каждого
дневника свой - на один шаг разговора. Правило простое и его нельзя нарушать:
под замком не делаем сетевых вызовов. Ни запроса к модели, ни отправки в
Telegram - иначе один медленный ответ сервиса останавливает всех остальных.
Запись на диск под замком - можно: она короткая (один изменившийся день) и
никуда не ходит.
"""
from __future__ import annotations

import random
import re
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

from .derive import derive_factors
from .habits import imply_absences
from .insights import (OBSERVATION_MIN_DAYS, OBSERVATION_PERMUTATIONS, Link,
                       find_links)
from .schema import DayRecord, Fact, Timeline
from .storage import Storage

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

# Сколько реплик заданный вопрос ждёт ответа. Без срока он висит вечно и ловит
# любое число: «выпил 2 кофе» назавтра становилось оценкой вчерашней тревоги.
PENDING_TURNS = 3

_secrets = random.SystemRandom()


class Diary:
    """
    Один дневник: записи по дням, вопрос, на который ждём ответа, и лента
    сообщений для показа на странице.
    """

    def __init__(self, key: str, storage: Storage | None = None) -> None:
        self.key = key
        # Куда дневник сохраняется. Без хранилища он живёт только в памяти -
        # так работают тесты и так работал весь прототип до появления файла.
        self._storage = storage
        self.timeline = Timeline()
        # Город человека - всё, что нужно для погоды. Пустой город - погоды
        # просто нет, и дневник работает как раньше.
        self.city = ""
        self.pending: str | None = None      # заданный вопрос, ждём на него ответ
        self.pending_day: date | None = None  # день, про который спрашивали
        self.pending_turns = 0               # сколько реплик вопрос уже ждёт
        self.asked: set[str] = set()         # чтобы не спрашивать одно и то же
        # Привычки, про которые уже сказали, что о них известно. Пока набор тот
        # же, повторять вывод незачем: слово в слово на каждую реплику он
        # читается как заевшая пластинка.
        self.concluded: frozenset[str] | None = None
        # Наблюдения, уже сказанные вслух: повторять их каждой фразой - шум
        self.observed: set[str] = set()
        # День, когда говорили про длительное отклонение (`wam/drift.py`).
        # Фраза про месяц ниже своей нормы тяжёлая, и человек должен слышать её
        # раз в несколько дней, а не в ответ на каждую запись.
        self.drift_said: date | None = None
        # День, по которому уже подвели итог: повторять его каждой
        # репликой незачем
        self.wrapped_day = None
        # День, в который уже спросили про пропущенные дни. Один вопрос за
        # разговор: человек вернулся в дневник, а его встречают допросом -
        # так во второй раз он уже не вернётся.
        self.gap_asked: date | None = None
        # День, в который человек попросил не спрашивать («достал ты со своими
        # вопросами»). До конца этого дня только записываем, молча.
        self.quiet_day: date | None = None
        # Что и в какой день записала прошлая реплика. Нужно для поправки:
        # «а нет, вру, это был вторник» - и запись надо перенести целиком.
        self.last_added: list[Fact] = []
        self.last_day: date | None = None
        self.messages: list[dict] = []
        self.seq = 0                         # номер последнего сообщения в ленте
        # Один шаг разговора за раз. Замок повторный: его берут и сами методы
        # дневника, и код снаружи - на обычном вложенный вызов повесил бы поток.
        self.lock = threading.RLock()
        self._links: list[Link] = []
        self._links_version: int | None = None
        self._hints: list[Link] = []
        self._hints_version: int | None = None

    # ── записи ────────────────────────────────────────────────────────────
    def today(self) -> DayRecord:
        """Запись за сегодня. Если её ещё нет - появится."""
        return self.record_for(date.today())

    def record_for(self, day: date) -> DayRecord:
        """
        Запись за названный день; если её ещё нет - появится. Нужна, когда
        человек рассказывает про прошедший день: «вчера пил вино» - это факт
        про вчера, и класть его в сегодняшний день нельзя.
        """
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

    def save(self) -> bool:
        """
        Сохранить записи и город. Звать надо после каждого шага, который что-то
        дописал в дневник: разговор человек может прервать в любой момент, а
        того, что не записано на диск, после перезапуска не существует.

        Дёшево: хранилище сравнивает дни со слепком прошлой записи и трогает
        только изменившийся день, поэтому лишний вызов ничего не стоит.

        Отвечает, лежит ли дневник на диске. Дневник без файла отвечает «нет»:
        он и не должен ничего сохранять, но и делать вид, что сохранил, ему
        нельзя - на этот ответ смотрят перед тем, как удалить старые данные.
        """
        if self._storage is None:
            return False
        with self.lock:
            return self._storage.save_diary(self.key, self.city, self.timeline.days)

    # ── вопрос, который ждёт ответа ───────────────────────────────────────
    def expect(self, question: str, day: date) -> None:
        """Запомнить заданный вопрос: про какой он день и сколько уже ждёт."""
        self.pending = question
        self.pending_day = day
        self.pending_turns = 0

    def pending_question(self, day: date) -> str | None:
        """
        Вопрос, ответа на который ещё имеет смысл ждать; просроченный гасим
        здесь же. Без срока вопрос ловил числа спустя сутки и приписывал оценку
        дню, который человек уже не оценивал.
        """
        if self.pending is None:
            return None
        if self.pending_day != day or self.pending_turns >= PENDING_TURNS:
            self.forget_question()
            return None
        return self.pending

    def wait_longer(self) -> None:
        """На вопрос не ответили - он ждёт ещё одну реплику."""
        self.pending_turns += 1

    def forget_question(self) -> None:
        """Вопрос снят: на него ответили или он устарел."""
        self.pending = None
        self.pending_day = None
        self.pending_turns = 0

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
                # Дни без привычки достраиваем здесь же: без них у привычки из
                # рассказа одни единицы, группа «без фактора» пустая и связь не
                # находится никогда - ни настоящая, ни ложная.
                imply_absences(self.timeline)
                self._links = find_links(derive_factors(self.timeline))
                # derive_factors и imply_absences дописывают факты, фактов после
                # этого больше - запоминаем именно итоговый слепок
                self._links_version = self._version()
            return self._links

    def hints(self) -> list[Link]:
        """
        Связи, набравшие всего по три дня в каждой группе.

        Выводами это не является, и продукт называет их человеку наблюдениями
        (см. `today.py`). Нужны они потому, что настоящая связь появляется
        недели через три, а смотреть на жизнь человека дневник должен с первой
        недели - иначе он просто форма ввода.

        Пары, по которым уже есть честная связь, отсюда убираем: сказать об
        одном и том же дважды, да ещё разными словами, - значит запутать.
        """
        with self.lock:
            # Заодно досчитает факторы прибора: слепок версии берём после этого
            known = {(link.factor, link.metric) for link in self.links()}
            if self._version() != self._hints_version:
                found = find_links(self.timeline,
                                   permutations=OBSERVATION_PERMUTATIONS,
                                   min_days=OBSERVATION_MIN_DAYS)
                self._hints = [l for l in found if (l.factor, l.metric) not in known]
                self._hints_version = self._version()
            return self._hints

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
            self.forget_question()
            self.asked = set()
            self.concluded = None
            self.observed = set()
            self.drift_said = None
            self.wrapped_day = None
            self.gap_asked = None
            self.quiet_day = None
            self.last_added = []
            self.last_day = None
            self.messages = []
            self._links_version = None
            self._hints_version = None
            # Сегодняшний день ушёл и из файла тоже: иначе после перезапуска
            # он вернулся бы, и кнопка «начать заново» ничего бы не значила.
            self.save()


class DiaryStore:
    """
    Все дневники процесса и привязка чатов Telegram к ним.

    Ключ дневника - строка: у страницы это "web", у непривязанного чата
    "tg:<chat_id>". Код привязки одноразовый и живёт четверть часа.

    `path` - файл, в котором дневники живут между запусками. Без него хранилище
    работает целиком в памяти: так его заводят тесты, и настоящий дневник
    человека они не трогают.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._lock = threading.RLock()
        self._diaries: dict[str, Diary] = {}
        self._codes: dict[str, tuple[str, float]] = {}     # код -> (ключ, когда выдан)
        self._chats: dict[int, str] = {}                   # чат -> ключ дневника
        self._misses: dict[int, tuple[int, float]] = {}    # чат -> (промахи, до когда молчим)
        self._misses_total = 0
        self._guests: dict[str, float] = {}                # дневник чата -> когда о нём вспоминали
        self._storage = self._open(path)
        if self._storage is not None:
            self._restore()

    @staticmethod
    def _open(path: Path | str | None) -> Storage | None:
        """
        Открыть файл дневника. Не вышло совсем - работаем в памяти: человек
        потеряет записи при перезапуске, но хотя бы сможет писать сегодня.
        Запуск с чистого листа, когда файла ещё нет, - это не «не вышло»,
        хранилище заводит файл само.
        """
        if path is None:
            return None
        try:
            return Storage(path)
        except (OSError, sqlite3.Error) as exc:
            print(f"Дневник на диске недоступен ({exc}). Записи будут жить "
                  "только до конца работы программы.")
            return None

    def _restore(self) -> None:
        """Поднять записи и привязки чатов с прошлого запуска."""
        saved = self._storage.load()
        for key, timeline in saved.timelines.items():
            diary = self.get(key)
            diary.timeline = timeline
        for key, city in saved.cities.items():
            self.get(key).city = city
        self._chats.update(saved.chats)
        # Дневники чатов, которые так и не привязали: срок жизни у них тот же,
        # что и был, поэтому вспоминаем, когда в них писали в последний раз.
        bound = set(self._chats.values())
        self._guests.update({key: seen for key, seen in saved.seen.items()
                             if key.startswith("tg:") and key not in bound})
        self._forget_guests()

    def get(self, key: str) -> Diary:
        """Дневник по ключу; один ключ - всегда один и тот же дневник."""
        with self._lock:
            diary = self._diaries.get(key)
            if diary is None:
                diary = Diary(key, self._storage)
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
            # Привязку тоже храним: после перезапуска человек в том же чате
            # должен остаться собой, а не начать писать в чужой пустой дневник.
            if self._storage is not None:
                self._storage.set_chat(chat_id, key)
            return key

    def code_is_live(self, code: str) -> bool:
        """
        Отзовётся ли бот на этот код. Спрашивать надо здесь: коды гаснут не
        только по времени - при переборе из многих чатов гаснут все разом, и
        тот, кто выдал код, об этом не узнает.
        """
        with self._lock:
            self._forget_stale()
            return self._normalise(code) in self._codes

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
                if self._storage is not None:
                    self._storage.drop_chat(chat)
            self._codes = {code: value for code, value in self._codes.items()
                           if value[0] != key}
            return chats

    def unlink_chat(self, chat_id: int, key: str) -> bool:
        """
        Отвязать один чат от этого дневника. Записи остаются в дневнике
        страницы: человек их не терял, он закрыл доступ. Дальше этот чат снова
        пишет в свой дневник.

        Ключ дневника спрашиваем не зря: отвязывать можно только свой чат,
        чужой номер в запросе не должен рвать чужую привязку.
        """
        with self._lock:
            if self._chats.get(chat_id) != key:
                return False
            del self._chats[chat_id]
            if self._storage is not None:
                self._storage.drop_chat(chat_id)
            return True

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
            target.save()
        # Прежний дневник чата уехал целиком - в файле ему делать нечего,
        # иначе после перезапуска записи снова раздвоятся.
        if self._storage is not None:
            self._storage.drop_diary(from_key)

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
            if self._storage is not None:
                self._storage.drop_diary(key)

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
