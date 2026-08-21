"""
Дневник на диске: записи переживают перезапуск программы.

Человек ведёт дневник неделями, и терять его при закрытии программы нельзя.
Здесь лежит только то, что должно пережить перезапуск: дни с фактами, город и
привязка чатов Telegram к дневникам. Лента сообщений, заданный вопрос и коды
привязки сюда не попадают - это состояние разговора, а не дневник, и после
перезапуска разговор честно начинается заново.

Почему SQLite, а не JSON. Дневник дописывается на каждую реплику, а читается
целиком один раз - при запуске. JSON пришлось бы переписывать целиком на каждое
слово человека: на полугоде записей это сотни килобайт на фразу, и половина
записанного файла после внезапной остановки - уже не дневник, а мусор. SQLite
из той же стандартной библиотеки переписывает один изменившийся день, а коммит
у него атомарный: программу убили посреди записи - файл остался целым. Новых
зависимостей это не требует, а значит, прототип по-прежнему запускается одной
командой.

Чего тут нет: шифрования. Заказчик его пока не выбирал, и делать вид, что файл
защищён, нечестно. Что есть: права только владельцу (0600) и запрет на попадание
в репозиторий (см. .gitignore).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .schema import DayRecord, Fact, Timeline

# Файл дневника лежит рядом с программой, и путь к нему назван один раз - здесь.
# Всё остальное берёт его отсюда, чтобы дневник не расползся по двум файлам.
DIARY_FILE = Path(__file__).resolve().parent.parent / ".diary.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS diaries (
    diary TEXT PRIMARY KEY,
    city  TEXT NOT NULL DEFAULT '',
    seen  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS days (
    diary    TEXT NOT NULL,
    day      TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (diary, day)
);
CREATE TABLE IF NOT EXISTS facts (
    diary  TEXT NOT NULL,
    day    TEXT NOT NULL,
    pos    INTEGER NOT NULL,
    kind   TEXT NOT NULL,
    name   TEXT NOT NULL,
    value  REAL NOT NULL,
    source TEXT NOT NULL,
    quote  TEXT NOT NULL,
    PRIMARY KEY (diary, day, pos)
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    diary   TEXT NOT NULL
);
"""


@dataclass
class Saved:
    """Что нашлось в файле при запуске."""

    timelines: dict[str, Timeline] = field(default_factory=dict)
    cities: dict[str, str] = field(default_factory=dict)
    chats: dict[int, str] = field(default_factory=dict)
    # Когда в дневник писали в последний раз - по этому сроку хранилище
    # выкидывает дневники случайных чатов, которые никто не привязывал.
    seen: dict[str, float] = field(default_factory=dict)


def _snapshot(record: DayRecord) -> tuple:
    """Слепок дня: по нему видно, менялся ли день с прошлой записи на диск."""
    return (record.raw_text,
            tuple((f.kind, f.name, f.value, f.source, f.quote) for f in record.facts))


class Storage:
    """
    Файл дневника. Одно соединение на процесс под своим замком: страница, бот и
    напоминания пишут из разных потоков.

    Замок хранилища всегда самый внутренний: сюда заходят уже под замком
    дневника, а сам он ничьих замков не берёт - иначе порядок замков в проекте
    (хранилище дневников → дневник → файл) развернулся бы и свёл потоки намертво.
    """

    def __init__(self, path: Path | str = DIARY_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db = self._open()
        # Что уже записано: дневник → день → слепок. Нужно, чтобы на каждую
        # реплику писать один изменившийся день, а не всю историю.
        self._days: dict[str, dict[str, tuple]] = {}
        self._cities: dict[str, str] = {}
        self._complained = False

    # ── открытие файла ────────────────────────────────────────────────────
    def _open(self) -> sqlite3.Connection:
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:
            # Файл битый или недописанный. Начать с пустого дневника и сказать
            # об этом лучше, чем не запуститься вовсе. Старый файл не удаляем -
            # отодвигаем в сторону: это чужие записи, вдруг их ещё достанут.
            spoiled = self.path.with_name(self.path.name + ".broken")
            print(f"Файл дневника не читается ({exc}). Отложил его в "
                  f"{spoiled.name} и начинаю с пустого дневника.")
            os.replace(self.path, spoiled)
            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            # Файл создаём сами и сразу с нужными правами: если довериться
            # sqlite, он на мгновение полежит открытым для всей машины.
            # Пустой файл для sqlite - это пустая база, а не поломка.
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        # Дневник - самое личное, что есть у человека: читать его может только
        # он. Права поправляем и на уже существующем файле. Журнал sqlite
        # наследует права базы, отдельно за ним следить не нужно.
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.executescript(_SCHEMA)
        return db

    # ── чтение при запуске ────────────────────────────────────────────────
    def load(self) -> Saved:
        """
        Всё, что лежит в файле. Читается один раз, при запуске.

        Строка, которую не удалось разобрать (кривая дата, факт без имени),
        пропускается молча: одна испорченная запись не стоит потерянного
        дневника.
        """
        saved = Saved()
        with self._lock:
            try:
                rows = self._db.execute(
                    "SELECT diary, city, seen FROM diaries").fetchall()
                days = self._db.execute(
                    "SELECT diary, day, raw_text FROM days ORDER BY diary, day").fetchall()
                facts = self._db.execute(
                    "SELECT diary, day, kind, name, value, source, quote "
                    "FROM facts ORDER BY diary, day, pos").fetchall()
                chats = self._db.execute("SELECT chat_id, diary FROM chats").fetchall()
            except sqlite3.DatabaseError as exc:
                print(f"Дневник с прошлого запуска прочитать не вышло ({exc}). "
                      "Начинаю с пустого.")
                return saved

            for key, city, seen in rows:
                saved.cities[key] = city or ""
                saved.seen[key] = float(seen or 0)

            records: dict[tuple[str, str], DayRecord] = {}
            for key, day, raw_text in days:
                try:
                    record = DayRecord(day=date.fromisoformat(day), raw_text=raw_text or "")
                except ValueError:
                    continue
                records[(key, day)] = record
                saved.timelines.setdefault(key, Timeline()).add(record)

            for key, day, kind, name, value, source, quote in facts:
                record = records.get((key, day))
                if record is None or not name:
                    continue
                record.facts.append(Fact(kind=kind, name=name, value=float(value),
                                         source=source or "", quote=quote or ""))

            for chat_id, key in chats:
                saved.chats[int(chat_id)] = key

            # Запоминаем прочитанное как уже записанное, иначе первая же реплика
            # перепишет на диск всю историю целиком.
            for key, timeline in saved.timelines.items():
                self._days[key] = {r.day.isoformat(): _snapshot(r) for r in timeline.days}
            self._cities.update(saved.cities)
        return saved

    # ── запись ────────────────────────────────────────────────────────────
    def save_diary(self, key: str, city: str, days: list[DayRecord]) -> bool:
        """
        Записать дневник. Пишем только то, что изменилось: за реплику меняется
        один день, и перебирать ради него полугодовую историю незачем.

        Отвечает, лежит ли теперь дневник на диске. Спрашивать это нужно редко -
        например, перед тем как убрать старый файл с городом, - но узнать про
        неудачу должно быть можно, а не только прочитать про неё в логе.
        """
        records = {record.day.isoformat(): record for record in days}
        shots = {day: _snapshot(record) for day, record in records.items()}
        with self._lock:
            known = self._days.get(key, {})
            changed = [day for day, shot in shots.items() if known.get(day) != shot]
            gone = [day for day in known if day not in shots]
            fresh_city = self._cities.get(key, None) != city
            if not changed and not gone and not fresh_city:
                return True                     # всё это уже записано
            try:
                with self._db:
                    self._db.execute(
                        "INSERT OR REPLACE INTO diaries(diary, city, seen) VALUES(?,?,?)",
                        (key, city, time.time()))
                    for day in changed:
                        self._write_day(key, records[day])
                    for day in gone:
                        self._drop_day(key, day)
            except sqlite3.Error as exc:
                self._failed(exc)
                return False
            self._days[key] = shots
            self._cities[key] = city
            return True

    def _write_day(self, key: str, record: DayRecord) -> None:
        """Один день целиком: факты дня переписываем, а не досыпаем."""
        day = record.day.isoformat()
        self._db.execute("INSERT OR REPLACE INTO days(diary, day, raw_text) VALUES(?,?,?)",
                         (key, day, record.raw_text))
        self._db.execute("DELETE FROM facts WHERE diary=? AND day=?", (key, day))
        self._db.executemany(
            "INSERT INTO facts(diary, day, pos, kind, name, value, source, quote) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(key, day, pos, f.kind, f.name, float(f.value), f.source, f.quote)
             for pos, f in enumerate(record.facts)])

    def _drop_day(self, key: str, day: str) -> None:
        self._db.execute("DELETE FROM days WHERE diary=? AND day=?", (key, day))
        self._db.execute("DELETE FROM facts WHERE diary=? AND day=?", (key, day))

    def drop_diary(self, key: str) -> None:
        """Забыть дневник целиком: он переехал к другому ключу или устарел."""
        with self._lock:
            self._days.pop(key, None)
            self._cities.pop(key, None)
            try:
                with self._db:
                    self._db.execute("DELETE FROM facts WHERE diary=?", (key,))
                    self._db.execute("DELETE FROM days WHERE diary=?", (key,))
                    self._db.execute("DELETE FROM diaries WHERE diary=?", (key,))
            except sqlite3.Error as exc:
                self._failed(exc)

    # ── привязка чатов ────────────────────────────────────────────────────
    def set_chat(self, chat_id: int, key: str) -> None:
        with self._lock:
            try:
                with self._db:
                    self._db.execute("INSERT OR REPLACE INTO chats(chat_id, diary) VALUES(?,?)",
                                     (int(chat_id), key))
            except sqlite3.Error as exc:
                self._failed(exc)

    def drop_chat(self, chat_id: int) -> None:
        with self._lock:
            try:
                with self._db:
                    self._db.execute("DELETE FROM chats WHERE chat_id=?", (int(chat_id),))
            except sqlite3.Error as exc:
                self._failed(exc)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _failed(self, exc: Exception) -> None:
        """
        Записать не вышло - диск полон или файл забрали. Разговор из-за этого
        не рвём, но и молчать нельзя: человек должен знать, что его записи
        сейчас живут только в памяти. Жалуемся один раз, а не на каждую фразу.
        """
        if not self._complained:
            self._complained = True
            print(f"Дневник не сохраняется на диск: {exc}")
