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
import threading
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

# Заведённые на сайте без согласия живут сутки, а не неделю: их можно
# наштамповать кнопкой, и неделя копила бы тысячи пустых записей.
SITE_CONSENT_WAIT_DAYS = 1

# Сколько scrypt считать одновременно. Каждый - 16 МиБ и десятки миллисекунд;
# без потолка шквал входов по коду съел бы память контейнера (384 МиБ) и все
# потоки. Кто не дождался места за SCRYPT_WAIT секунд, получает Busy (503).
SCRYPT_SLOTS = threading.BoundedSemaphore(2)
SCRYPT_WAIT = 2

# Соль для проверки, когда по отпечатку никого не нашлось: scrypt считаем и
# тогда, чтобы по времени ответа нельзя было понять, совпал ли отпечаток.
_DUMMY_SALT = "00" * 16


class Busy(Exception):
    """Все места для scrypt заняты: попросить человека повторить чуть позже."""


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
    if not SCRYPT_SLOTS.acquire(timeout=SCRYPT_WAIT):
        raise Busy()
    try:
        return hashlib.scrypt(code.encode("utf-8"), salt=bytes.fromhex(salt), **SCRYPT).hex()
    finally:
        SCRYPT_SLOTS.release()


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

    def agree_in_chat(self, user_id: int, version: str = CONSENT_VERSION) -> None:
        """Согласие нажато в самом чате Telegram: оно же и общее согласие."""
        with self.storage.atomic() as db:
            db.execute("UPDATE users SET consent_at=?, consent_version=?, "
                       "tg_consent_version=? WHERE user_id=?",
                       (time.time(), version, version, int(user_id)))

    def has_chat_consent(self, user_id: int) -> bool:
        """
        Можно ли записывать то, что пишут в чат Telegram этого человека.

        Кто пришёл из Telegram (user_id == tg_id), тот и есть хозяин чата:
        его согласия, данного где угодно, достаточно. Кто начал на сайте и
        подключил Telegram потом, должен подтвердить согласие в самом чате -
        иначе в дневник писал бы чат, за которого согласился кто-то другой.
        """
        if not self.has_consent(user_id):
            return False
        with self.storage.atomic() as db:
            row = db.execute("SELECT tg_id, tg_consent_version FROM users WHERE user_id=?",
                             (int(user_id),)).fetchone()
        return bool(row) and (row[0] == int(user_id) or row[1] == CONSENT_VERSION)

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
        выдаём новый, пока человек ещё в дневнике. Повтор кода у двух людей
        при 79 битах не проверяем: вероятность ничтожна, а проверка стоила
        бы ещё одного scrypt. Busy - все места для scrypt заняты.
        """
        letters = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        code = normal_code(letters)
        salt = secrets.token_hex(16)
        digest = _code_hash(code, salt)
        with self.storage.atomic() as db:
            db.execute("UPDATE users SET code_index=?, code_salt=?, code_hash=? "
                       "WHERE user_id=?", (_code_index(code), salt, digest, int(user_id)))
        return code

    def redeem_code(self, code) -> int | None:
        """
        Чей это код. Неверный, кривой и несуществующий - одинаково None.
        Busy - все места для scrypt заняты.
        """
        code = normal_code(code)
        if not code:
            return None
        with self.storage.atomic() as db:
            rows = db.execute("SELECT user_id, code_salt, code_hash FROM users "
                              "WHERE code_index=?", (_code_index(code),)).fetchall()
        # scrypt считаем вне замка хранилища: десятки миллисекунд на каждого
        # кандидата, а под замком стоят все запросы сервиса. Никого не нашлось
        # - всё равно один scrypt, чтобы время ответа было одинаковым.
        if not rows:
            _code_hash(code, _DUMMY_SALT)
            return None
        found = None
        for user_id, salt, stored in rows:
            if salt and stored and hmac.compare_digest(_code_hash(code, salt), stored):
                found = int(user_id)
        return found

    # ── подключение Telegram со страницы ──────────────────────────────────
    # Два шага, а не один. Будь ссылка t.me/<бот>?start=<токен> сама по себе
    # привязкой, кто угодно выпустил бы её на своём дневнике и подсунул
    # другому человеку: тот нажал бы «Запустить», и всё, что он пишет боту,
    # уходило бы в чужой дневник. Поэтому бот лишь предлагает чат и шлёт в
    # него вторую ссылку - на страницу. Подтвердить её может только та же
    # сессия браузера, что выпустила первую: у подсунувшего ссылку второй
    # ссылки нет (она пришла в чужой чат), а у получившего - нет его сессии.
    def issue_link(self, user_id: int, sid: str) -> str:
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
            db.execute("INSERT INTO link_tokens(token_hash, user_id, expires_at, sid_hash) "
                       "VALUES(?,?,?,?)",
                       (_digest(token), int(user_id), now + LINK_LIFETIME, _digest(sid)))
        return token

    def offer_link(self, token, tg_id: int, username: str = "") -> tuple[str | None, str]:
        """
        Шаг бота: чат открыл ссылку t.me/...?start=<токен>. Ничего не
        привязываем, а выдаём второй токен - для ссылки на страницу. Возвращает
        (токен, "") или (None, причина): "bad" - ссылка не та, устарела или
        уже использована; "taken" - у этого Telegram уже свой дневник; "has" -
        к дневнику уже подключён Telegram.
        """
        if not _usable(token):
            return None, "bad"
        digest, tg_id, now = _digest(token), int(tg_id), time.time()
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id, expires_at, sid_hash FROM link_tokens "
                             "WHERE token_hash=? AND tg_id IS NULL", (digest,)).fetchone()
            if row is None:
                return None, "bad"
            db.execute("DELETE FROM link_tokens WHERE token_hash=?", (digest,))
            user_id, expires_at, sid_hash = int(row[0]), row[1], row[2]
            if expires_at <= now or not sid_hash:
                return None, "bad"
            problem = self._link_problem(db, user_id, tg_id)
            if problem:
                return None, problem
            second = secrets.token_urlsafe(32)
            db.execute("INSERT INTO link_tokens(token_hash, user_id, expires_at, sid_hash, "
                       "tg_id, username) VALUES(?,?,?,?,?,?)",
                       (_digest(second), user_id, now + LINK_LIFETIME, sid_hash,
                        tg_id, (username or "")[:64]))
        return second, ""

    def confirm_link(self, token, user_id: int, sid: str) -> tuple[int | None, str]:
        """
        Шаг страницы: человек открыл вторую ссылку в своём браузере. Годится
        только для того же человека и той же сессии, что выпустили первую.
        Возвращает (номер чата, "") или (None, причина) - как offer_link.
        Токен гаснет при первой же попытке, в том числе чужой.
        """
        if not _usable(token) or not _usable(sid):
            return None, "bad"
        digest = _digest(token)
        with self.storage.atomic() as db:
            row = db.execute("SELECT user_id, expires_at, sid_hash, tg_id, username "
                             "FROM link_tokens WHERE token_hash=? AND tg_id IS NOT NULL",
                             (digest,)).fetchone()
            if row is None:
                return None, "bad"
            db.execute("DELETE FROM link_tokens WHERE token_hash=?", (digest,))
            owner, expires_at, sid_hash, tg_id, username = row
            if (expires_at <= time.time() or int(owner) != int(user_id)
                    or not hmac.compare_digest(sid_hash, _digest(sid))):
                return None, "bad"
            # Между шагами могло что-то поменяться: проверяем заново
            problem = self._link_problem(db, int(user_id), int(tg_id))
            if problem:
                return None, problem
            # Согласие, данное на сайте, на чат не переносится: его надо
            # подтвердить в самом чате (has_chat_consent).
            db.execute("UPDATE users SET tg_id=?, username=?, tg_consent_version=NULL, "
                       "active_at=? WHERE user_id=?",
                       (int(tg_id), username, time.time(), int(user_id)))
        return int(tg_id), ""

    @staticmethod
    def _link_problem(db, user_id: int, tg_id: int) -> str:
        owner = db.execute("SELECT tg_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if owner is None:
            return "bad"                    # человека удалили, пока ссылка шла
        if owner[0] is not None:
            return "has"
        if db.execute("SELECT 1 FROM users WHERE tg_id=?", (tg_id,)).fetchone():
            # Склеивать два дневника молча нельзя: записи одного легли
            # бы к другому. Пусть человек сам решит, какой оставить.
            return "taken"
        return ""

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

    def close_other_sessions(self, user_id: int, keep_sid: str) -> None:
        """Закрыть все сессии человека, кроме этой: после смены кода."""
        with self.storage.atomic() as db:
            db.execute("DELETE FROM sessions WHERE user_id=? AND sid_hash<>?",
                       (int(user_id), _digest(keep_sid)))

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
                    now: float | None = None,
                    site_days: int = SITE_CONSENT_WAIT_DAYS) -> list[int]:
        """
        Кто завёл учётную запись и так и не согласился: из бота - дольше
        `days` дней назад, с сайта (user_id < 0) - дольше `site_days`.
        """
        now = time.time() if now is None else now
        edge, site_edge = now - days * 86400, now - site_days * 86400
        with self.storage.atomic() as db:
            rows = db.execute("SELECT user_id FROM users WHERE consent_at IS NULL "
                              "AND (created_at < ? OR (user_id < 0 AND created_at < ?))",
                              (edge, site_edge)).fetchall()
        return [int(row[0]) for row in rows]

    def waiting_consent(self) -> int:
        """Сколько учётных записей ещё без согласия - для потолка новых."""
        with self.storage.atomic() as db:
            return db.execute("SELECT COUNT(*) FROM users "
                              "WHERE consent_at IS NULL").fetchone()[0]

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
