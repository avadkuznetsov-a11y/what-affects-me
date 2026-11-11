"""
Люди сервиса: учётные записи, вход по ссылке от бота, сессии и согласие.

Нужно только в режиме server (MIRA_MODE=server), где у каждого свой дневник.
В режиме local человек один, входа нет, и этот модуль не участвует.

Человек - это его номер в Telegram: при `/start` бот заводит запись по
`from.id`, и дневник получает ключ `u:<номер>`. Других данных о человеке тут
нет, кроме имени в Telegram (его показывает страница) и отметки о согласии.

Чего здесь никогда не лежит в открытом виде - ни токена входа, ни ключа
сессии. В базе только их sha256: утечка файла не даёт войти ни в один дневник.
Токен случайный, 32 байта, поэтому соль и медленный хэш ему не нужны - перебрать
такое пространство нельзя, в отличие от пароля, который придумал человек.
"""
from __future__ import annotations

import hashlib
import secrets
import time

from .storage import Storage

# Ссылка для входа живёт десять минут и срабатывает один раз: это ключ от
# дневника, пролежавший в переписке.
LOGIN_LIFETIME = 10 * 60

# Сессия скользящая: месяц с последнего захода, а не с первого входа. Кто
# пользуется дневником, не должен каждый месяц заново ходить к боту.
SESSION_LIFETIME = 30 * 24 * 60 * 60

# Отметку «был тут» пишем не на каждый запрос: страница опрашивает сервер раз
# в две секунды, и запись на диск на каждый опрос - пустая работа. Часа хватает,
# чтобы срок в тридцать дней считался честно.
SESSION_TOUCH = 60 * 60

# Длиннее настоящего токена ничего не бывает; длинную строку незачем и хэшировать.
MAX_SECRET = 100

# Версия текста согласия. Поменялся текст - меняем версию, и каждый при
# следующем входе соглашается заново: согласие даётся на конкретный текст.
# Сейчас текст - черновик (см. web/privacy.html), оператор и реквизиты
# уточняются, поэтому и версия помечена как черновая.
CONSENT_VERSION = "2026-11-11-черновик"

COOKIE = "__Host-mira_sid"


def user_key(user_id: int) -> str:
    """Ключ дневника человека. Один номер - всегда один и тот же дневник."""
    return f"u:{int(user_id)}"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def csrf_for(sid: str) -> str:
    """
    Токен CSRF выводится из ключа сессии, а не хранится отдельно. Ключ сессии
    знают только браузер человека и сервер в момент запроса, поэтому чужая
    страница токена не угадает, а в базе нечего красть: там лишь sha256 сессии,
    из которого этот токен не получить.
    """
    return hashlib.sha256(("csrf:" + sid).encode("utf-8")).hexdigest()[:32]


def _usable(secret) -> bool:
    return isinstance(secret, str) and 0 < len(secret) <= MAX_SECRET


class Accounts:
    """
    Учёт людей поверх файла дневников. Своих замков нет: всё идёт через
    `Storage.atomic()`, то есть под замком хранилища, самым внутренним в
    проекте. Поэтому звать эти методы можно из-под замка дневника.
    """

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    # ── люди ──────────────────────────────────────────────────────────────
    def enroll(self, user_id: int, username: str = "") -> None:
        """Завести человека или обновить его имя в Telegram. Согласие не трогаем."""
        with self.storage.atomic() as db:
            db.execute(
                "INSERT INTO users(user_id, username, created_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
                (int(user_id), (username or "")[:64], time.time()))

    def user(self, user_id: int) -> dict | None:
        with self.storage.atomic() as db:
            row = db.execute(
                "SELECT user_id, username, consent_at, consent_version "
                "FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "username": row[1],
                "consent_at": row[2], "consent_version": row[3]}

    def count(self) -> int:
        with self.storage.atomic() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    # ── согласие ──────────────────────────────────────────────────────────
    def agree(self, user_id: int, version: str = CONSENT_VERSION) -> None:
        with self.storage.atomic() as db:
            db.execute("UPDATE users SET consent_at=?, consent_version=? WHERE user_id=?",
                       (time.time(), version, int(user_id)))

    def has_consent(self, user_id: int) -> bool:
        """Согласие есть, только если дано на нынешний текст."""
        user = self.user(user_id)
        return bool(user and user["consent_at"] and user["consent_version"] == CONSENT_VERSION)

    # ── вход по ссылке ────────────────────────────────────────────────────
    def issue_login(self, user_id: int) -> str:
        """
        Новый токен для ссылки входа. Прежние токены этого человека гаснут:
        действует только последняя ссылка, и старая, забытая в переписке,
        больше никуда не ведёт.
        """
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.storage.atomic() as db:
            db.execute("DELETE FROM login_tokens WHERE expires_at<=? OR user_id=?",
                       (now, int(user_id)))
            db.execute("INSERT INTO login_tokens(token_hash, user_id, expires_at) "
                       "VALUES(?,?,?)", (_digest(token), int(user_id), now + LOGIN_LIFETIME))
        return token

    def redeem_login(self, token) -> int | None:
        """
        Погасить токен и сказать, чей он. Поиск и удаление - в одной
        транзакции: два одновременных входа по одной ссылке не пройдут оба.
        """
        if not _usable(token):
            return None
        digest = _digest(token)
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id, expires_at FROM login_tokens "
                             "WHERE token_hash=?", (digest,)).fetchone()
            if row is None:
                return None
            db.execute("DELETE FROM login_tokens WHERE token_hash=?", (digest,))
            user_id, expires_at = row
            if expires_at <= time.time():
                return None
            # Человека могли удалить, пока ссылка лежала в переписке
            alive = db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(user_id) if alive else None

    # ── сессии ────────────────────────────────────────────────────────────
    def open_session(self, user_id: int) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE seen_at<=?", (now - SESSION_LIFETIME,))
            db.execute("INSERT INTO sessions(sid_hash, user_id, created_at, seen_at) "
                       "VALUES(?,?,?,?)", (_digest(sid), int(user_id), now, now))
        return sid

    def session_user(self, sid) -> int | None:
        """Чья это сессия. Просроченная гаснет здесь же, живая продлевается."""
        if not _usable(sid):
            return None
        digest = _digest(sid)
        now = time.time()
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id, seen_at FROM sessions WHERE sid_hash=?",
                             (digest,)).fetchone()
            if row is None:
                return None
            user_id, seen_at = row
            if seen_at <= now - SESSION_LIFETIME:
                db.execute("DELETE FROM sessions WHERE sid_hash=?", (digest,))
                return None
            if seen_at <= now - SESSION_TOUCH:
                db.execute("UPDATE sessions SET seen_at=? WHERE sid_hash=?", (now, digest))
        return int(user_id)

    def close_session(self, sid) -> None:
        if not _usable(sid):
            return
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE sid_hash=?", (_digest(sid),))

    # ── удаление ──────────────────────────────────────────────────────────
    def erase(self, user_id: int, store) -> None:
        """
        Удалить человека целиком: учётную запись, сессии, ссылки входа,
        напоминания, привязку чатов и сам дневник.

        Сначала учёт, потом дневник. Пока дневник стирается, бот и страница не
        должны успеть записать в него снова: без учётной записи бот новую
        реплику не принимает, а без сессии страница - запрос.

        `store` - хранилище дневников (wam.diary.DiaryStore). Принимаем его
        параметром, а не импортом: дневники про учёт людей знать не должны.
        """
        uid = int(user_id)
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            db.execute("DELETE FROM login_tokens WHERE user_id=?", (uid,))
            # В личном чате номер чата совпадает с номером человека
            db.execute("DELETE FROM reminders WHERE chat_id=?", (uid,))
            db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        store.forget(user_key(uid))
