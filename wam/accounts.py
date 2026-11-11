"""
Люди сервиса: учётные записи, вход по ссылке от бота, сессии и согласие.

Нужно только в режиме server (MIRA_MODE=server), где у каждого свой дневник.
В режиме local человек один, входа нет, и этот модуль не участвует.

Человек приходит одним из двух путей:
- из Telegram: при `/start` бот заводит запись по `from.id`, и этот номер
  становится и user_id, и tg_id;
- с сайта, кнопкой «Начать без Telegram»: user_id отрицательный и случайный
  (номера в Telegram всегда положительные, поэтому пересечься они не могут),
  tg_id пустой. Входит такой человек по личному коду, а Telegram может
  подключить потом одноразовой ссылкой на бота.
Дневник получает ключ `u:<user_id>`. Других данных о человеке тут нет, кроме
имени в Telegram (его показывает страница) и отметки о согласии.

Чего здесь никогда не лежит в открытом виде - ни токена входа, ни ключа
сессии. В базе только их sha256: утечка файла не даёт войти ни в один дневник.
Токен случайный, 32 байта, поэтому соль и медленный хэш ему не нужны - перебрать
такое пространство нельзя, в отличие от пароля, который придумал человек.

Личный код - другое дело: он короче (около 80 бит, чтобы его можно было
переписать руками) и живёт долго. Поэтому он хранится как scrypt с солью на
каждого человека, см. issue_code().
"""
from __future__ import annotations

import hashlib
import hmac
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
# 2026-11-11-черновик-2: в тексте появились вход без Telegram и личный код.
CONSENT_VERSION = "2026-11-11-черновик-2"

COOKIE = "__Host-mira_sid"

# Личный код: та же азбука, что у кода привязки в прототипе (wam.diary), без
# похожих знаков - 0/O, 1/I/L. 16 знаков из 31 - около 79 бит: перебрать
# онлайн нельзя даже без потолков, а с потолками и подавно.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 16
CODE_GROUP = 4

# scrypt: 16 МиБ памяти и десятки миллисекунд на одну проверку. Для человека
# незаметно, а утёкшую базу перебирать придётся на этой цене за каждую пробу.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}

# Сколько знаков sha256 кода храним для поиска. Соль у каждого своя, поэтому
# найти человека по коду одним запросом можно только по чему-то несолёному.
# Четыре шестнадцатеричных знака - 16 бит из 79: отпечаток сужает поиск до
# одного-двух человек, но почти ничего не говорит о самом коде.
CODE_INDEX = 4

# Ссылка «подключить Telegram» живёт столько же, сколько ссылка входа.
LINK_LIFETIME = 10 * 60

# Сколько хранится аккаунт, в который не входили и не писали. Решение
# владельца от 11.11.2026: год. Потом аккаунт удаляется целиком, как по
# кнопке «Удалить всё». Переопределяется переменной MIRA_RETENTION_DAYS.
RETENTION_DAYS = 365

# Сколько дней ждать согласия. Человек написал боту /start или нажал на сайте
# «Начать без Telegram» и не согласился - через неделю его учётная запись
# удаляется: хранить ПДн того, кто сервисом так и не стал пользоваться,
# незачем, а пустые записи с сайта иначе копились бы без конца.
CONSENT_WAIT_DAYS = 7


def user_key(user_id: int) -> str:
    """Ключ дневника человека. Один номер - всегда один и тот же дневник."""
    return f"u:{int(user_id)}"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normal_code(code) -> str:
    """
    Код так, как его выдали: человек перепишет его как получится - строчными,
    с пробелами, без тире или с длинным тире. Оставляем только знаки азбуки и
    расставляем тире сами. Не 16 знаков - пустая строка.
    """
    if not isinstance(code, str) or len(code) > MAX_SECRET:
        return ""
    letters = "".join(c for c in code.upper() if c in CODE_ALPHABET)
    if len(letters) != CODE_LENGTH:
        return ""
    return "-".join(letters[i:i + CODE_GROUP] for i in range(0, CODE_LENGTH, CODE_GROUP))


def _code_index(code: str) -> str:
    return hashlib.sha256(("code:" + code).encode("utf-8")).hexdigest()[:CODE_INDEX]


def _code_hash(code: str, salt: str) -> str:
    return hashlib.scrypt(code.encode("utf-8"), salt=bytes.fromhex(salt), **SCRYPT).hex()


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
    def enroll(self, tg_id: int, username: str = "") -> int:
        """
        Человек написал боту /start: найти его по номеру в Telegram или завести.
        Имя в Telegram обновляем, согласие не трогаем. Возвращает user_id -
        у того, кто начал на сайте и потом подключил Telegram, он не равен
        номеру в Telegram.
        """
        tg_id, name, now = int(tg_id), (username or "")[:64], time.time()
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if row is not None:
                db.execute("UPDATE users SET username=?, active_at=? WHERE user_id=?",
                           (name, now, row[0]))
                return int(row[0])
            db.execute(
                "INSERT INTO users(user_id, tg_id, username, created_at, active_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "tg_id=excluded.tg_id, username=excluded.username, "
                "active_at=excluded.active_at",
                (tg_id, tg_id, name, now, now))
        return tg_id

    def create(self) -> int:
        """
        Завести человека без Telegram (кнопка «Начать без Telegram»). Номер
        отрицательный и случайный: с номерами из Telegram не пересечётся, а
        по соседнему номеру нельзя угадать, сколько людей у сервиса.
        """
        now = time.time()
        with self.storage.atomic() as db:
            while True:
                user_id = -1 - secrets.randbelow(2 ** 52)
                taken = db.execute("SELECT 1 FROM users WHERE user_id=?",
                                   (user_id,)).fetchone()
                if not taken:
                    break
            db.execute("INSERT INTO users(user_id, username, created_at, active_at) "
                       "VALUES(?,?,?,?)", (user_id, "", now, now))
        return user_id

    def by_telegram(self, tg_id) -> int | None:
        """Чей это чат в Telegram. Нет такого человека - None."""
        if tg_id is None:
            return None
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id FROM users WHERE tg_id=?",
                             (int(tg_id),)).fetchone()
        return int(row[0]) if row else None

    def user(self, user_id: int) -> dict | None:
        with self.storage.atomic() as db:
            row = db.execute(
                "SELECT user_id, username, consent_at, consent_version, tg_id, code_hash "
                "FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        if row is None:
            return None
        return {"user_id": row[0], "username": row[1],
                "consent_at": row[2], "consent_version": row[3],
                "tg_id": row[4], "has_code": bool(row[5])}

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

    # ── личный код ────────────────────────────────────────────────────────
    def issue_code(self, user_id: int) -> str:
        """
        Новый личный код. Прежний гаснет сразу: хранится только последний.
        Показать код ещё раз нельзя - у нас его нет, только хэш; потерял -
        выдаём новый, пока человек ещё в дневнике.
        """
        while True:
            letters = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            code = normal_code(letters)
            # Повтор кода у двух людей при 79 битах не случится, но если бы
            # случился, вход по нему открыл бы чужой дневник. Проверка дешёвая.
            if self._find_code(code) is None:
                break
        salt = secrets.token_hex(16)
        with self.storage.atomic() as db:
            db.execute("UPDATE users SET code_index=?, code_salt=?, code_hash=? "
                       "WHERE user_id=?",
                       (_code_index(code), salt, _code_hash(code, salt), int(user_id)))
        return code

    def redeem_code(self, code) -> int | None:
        """Чей это код. Неверный, кривой и несуществующий - одинаково None."""
        code = normal_code(code)
        if not code:
            return None
        return self._find_code(code)

    def _find_code(self, code: str) -> int | None:
        with self.storage.atomic() as db:
            rows = db.execute("SELECT user_id, code_salt, code_hash FROM users "
                              "WHERE code_index=?", (_code_index(code),)).fetchall()
        # scrypt считаем вне замка хранилища: десятки миллисекунд на каждого
        # кандидата, а под замком стоят все запросы сервиса.
        for user_id, salt, stored in rows:
            if salt and stored and hmac.compare_digest(_code_hash(code, salt), stored):
                return int(user_id)
        return None

    # ── подключение Telegram со страницы ──────────────────────────────────
    def issue_link(self, user_id: int) -> str:
        """
        Токен для ссылки t.me/<бот>?start=<токен>. Действует десять минут,
        один раз, и только последний выданный. 32 байта в base64url - 43
        знака, в предел Telegram для start (64 знака, буквы, цифры, _ и -)
        укладывается.
        """
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.storage.atomic() as db:
            db.execute("DELETE FROM link_tokens WHERE expires_at<=? OR user_id=?",
                       (now, int(user_id)))
            db.execute("INSERT INTO link_tokens(token_hash, user_id, expires_at) "
                       "VALUES(?,?,?)", (_digest(token), int(user_id), now + LINK_LIFETIME))
        return token

    def link_telegram(self, token, tg_id: int, username: str = "") -> tuple[int | None, str]:
        """
        Привязать чат Telegram к человеку, который выдал ссылку на странице.
        Возвращает (user_id, "") или (None, причина): "bad" - ссылка не та,
        устарела или уже использована; "taken" - этот Telegram уже ведёт
        другой дневник; "has" - у человека уже подключён другой Telegram.

        Всё одной транзакцией, токен гаснет при первой же попытке: две
        вкладки с одной ссылкой не привяжут два чата.
        """
        if not _usable(token):
            return None, "bad"
        digest, tg_id = _digest(token), int(tg_id)
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id, expires_at FROM link_tokens "
                             "WHERE token_hash=?", (digest,)).fetchone()
            if row is None:
                return None, "bad"
            db.execute("DELETE FROM link_tokens WHERE token_hash=?", (digest,))
            user_id, expires_at = int(row[0]), row[1]
            if expires_at <= time.time():
                return None, "bad"
            owner = db.execute("SELECT tg_id FROM users WHERE user_id=?",
                               (user_id,)).fetchone()
            if owner is None:
                return None, "bad"          # человека удалили, пока ссылка шла
            if owner[0] == tg_id:
                return user_id, ""          # уже привязан к нему же - не беда
            if owner[0] is not None:
                return None, "has"
            other = db.execute("SELECT 1 FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            if other is not None:
                # Склеивать два дневника молча нельзя: записи одного легли
                # бы к другому. Пусть человек сам решит, какой оставить.
                return None, "taken"
            db.execute("UPDATE users SET tg_id=?, username=?, active_at=? WHERE user_id=?",
                       (tg_id, (username or "")[:64], time.time(), user_id))
        return user_id, ""

    # ── сессии ────────────────────────────────────────────────────────────
    def open_session(self, user_id: int) -> str:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE seen_at<=?", (now - SESSION_LIFETIME,))
            db.execute("INSERT INTO sessions(sid_hash, user_id, created_at, seen_at) "
                       "VALUES(?,?,?,?)", (_digest(sid), int(user_id), now, now))
            db.execute("UPDATE users SET active_at=? WHERE user_id=?", (now, int(user_id)))
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
                db.execute("UPDATE users SET active_at=? WHERE user_id=?", (now, user_id))
        return int(user_id)

    def close_session(self, sid) -> None:
        if not _usable(sid):
            return
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE sid_hash=?", (_digest(sid),))

    def close_all_sessions(self, user_id: int) -> None:
        """Выйти везде: вход, забытый на чужом компьютере, гаснет вместе с этим."""
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))

    # ── срок хранения ─────────────────────────────────────────────────────
    def idle(self, days: int, now: float | None = None) -> list[int]:
        """
        Кто не входил на страницу и ничего не записывал дольше `days` дней.

        Вход - это active_at: его ставят /start и /login в боте, вход по
        ссылке и заходы на страницу (раз в час, вместе с продлением сессии).
        Записи - это отметка дневника diaries.seen: её ставит каждое
        сохранение, откуда бы ни пришла запись. Нет ни того, ни другого -
        считаем от заведения аккаунта.
        """
        edge = (time.time() if now is None else now) - days * 24 * 60 * 60
        with self.storage.atomic() as db:
            rows = db.execute(
                "SELECT u.user_id FROM users u "
                "LEFT JOIN diaries d ON d.diary = 'u:' || u.user_id "
                "WHERE MAX(COALESCE(u.active_at, u.created_at), u.created_at, "
                "          COALESCE(d.seen, 0)) < ?", (edge,)).fetchall()
        return [int(row[0]) for row in rows]

    def unconsented(self, days: int = CONSENT_WAIT_DAYS,
                    now: float | None = None) -> list[int]:
        """Кто завёл учётную запись дольше `days` дней назад и так и не согласился."""
        edge = (time.time() if now is None else now) - days * 24 * 60 * 60
        with self.storage.atomic() as db:
            rows = db.execute("SELECT user_id FROM users "
                              "WHERE consent_at IS NULL AND created_at < ?", (edge,)).fetchall()
        return [int(row[0]) for row in rows]

    def orphans(self) -> list[str]:
        """
        Дневники людей (u:<номер>), у которых нет учётной записи. Так бывает,
        если запись в дневник легла в гонке с удалением человека или удаление
        когда-то оборвалось. Чужого в них ничего нет, но и хозяина уже нет -
        их стирает чистка по сроку.
        """
        with self.storage.atomic() as db:
            rows = db.execute(
                "SELECT diary FROM diaries WHERE diary LIKE 'u:%' "
                "AND NOT EXISTS (SELECT 1 FROM users "
                "                WHERE 'u:' || users.user_id = diaries.diary) "
                "UNION SELECT DISTINCT diary FROM days WHERE diary LIKE 'u:%' "
                "AND NOT EXISTS (SELECT 1 FROM users "
                "                WHERE 'u:' || users.user_id = days.diary)").fetchall()
        return sorted(row[0] for row in rows)

    # ── удаление ──────────────────────────────────────────────────────────
    def erase(self, user_id: int, store) -> None:
        """
        Удалить человека целиком: учётную запись, сессии, ссылки входа,
        напоминания, привязку чатов и сам дневник.

        Всё одной транзакцией sqlite: или удалено всё, или ничего, и тогда
        ошибка долетает до вызвавшего (страница отвечает 500, бот - «не
        получилось»). Пока дневник стирается, его держит замок дневника, а
        после - без учётной записи бот новую реплику не примет, без сессии
        страница не примет запрос.

        `store` - хранилище дневников (wam.diary.DiaryStore). Принимаем его
        параметром, а не импортом: дневники про учёт людей знать не должны.
        """
        uid = int(user_id)

        def account(db):
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            db.execute("DELETE FROM login_tokens WHERE user_id=?", (uid,))
            db.execute("DELETE FROM link_tokens WHERE user_id=?", (uid,))
            # Напоминания - по чату, а в личном чате номер чата совпадает с
            # номером человека в Telegram. Без Telegram их и не было.
            db.execute("DELETE FROM reminders WHERE chat_id IN "
                       "(SELECT tg_id FROM users WHERE user_id=?)", (uid,))
            db.execute("DELETE FROM users WHERE user_id=?", (uid,))

        # Одной транзакцией с дневником: ошибка на середине не оставит
        # человека «удалённым» без учётной записи, но с записями на диске.
        store.forget(user_key(uid), also=account)
