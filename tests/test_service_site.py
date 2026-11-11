"""
Режим server без Telegram: «Начать без Telegram», личный код, перебор кода,
смена кода, привязка Telegram к уже заведённому на сайте дневнику и запуск
сервиса без TELEGRAM_TOKEN.

Сервер и браузер - те же, что в test_service.py, бот с подменённым API - тот
же, что в test_service_bot.py.
"""
import json
import re
import sqlite3
import time

import pytest

import web.server as server
from bot.telegram import ERASE_WORD, TelegramBot
from wam import dialog
from wam.accounts import CODE_ALPHABET, normal_code, user_key

from test_service import ORIGIN, Person, service  # noqa: F401 - фикстура
from test_service_bot import FAKE_TOKEN, PUBLIC, FakeTelegram, _update

CODE_SHAPE = re.compile(rf"[{CODE_ALPHABET}]{{4}}(-[{CODE_ALPHABET}]{{4}}){{3}}")
WRONG = "Код не подошёл"


def _json_post(service, path, body, ip="10.0.0.1", **headers):
    sent = {"Origin": ORIGIN, "Content-Type": "application/json", "X-Real-IP": ip}
    sent.update(headers)
    return service.request("POST", path, body, sent)


def _sid(headers) -> str:
    return headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]


def newcomer(service, ip="10.0.0.1", consent=True) -> tuple[Person, str]:
    """Человек нажал «Начать без Telegram»: браузер с сессией и его код."""
    code, headers, body = _json_post(service, "/auth/new", {}, ip)
    assert code == 200, body
    who = Person(service, None)
    who.sid = _sid(headers)
    who.user_id = service.accounts.session_user(who.sid)
    if consent:
        assert who.post("/consent", {"agree": True})[0] == 200
    return who, json.loads(body)["code"]


def by_code(service, code, ip="10.0.0.2") -> tuple[int, Person | None, str]:
    """Вход по коду с другого устройства."""
    status, headers, body = _json_post(service, "/auth/code", {"code": code}, ip)
    if status != 200:
        return status, None, body
    who = Person(service, None)
    who.sid = _sid(headers)
    who.user_id = service.accounts.session_user(who.sid)
    return status, who, body


def _texts(state) -> str:
    return " ".join(m["text"] for m in state["messages"])


# ── «Начать без Telegram» ─────────────────────────────────────────────────

def test_start_without_telegram_opens_consent_then_diary(service):
    status, headers, body = _json_post(service, "/auth/new", {})
    assert status == 200, body
    code = json.loads(body)["code"]
    assert CODE_SHAPE.fullmatch(code)
    assert "HttpOnly" in headers["Set-Cookie"]

    who = Person(service, None)
    who.sid = _sid(headers)
    state = who.state()
    assert state["consent_needed"] is True            # сначала согласие
    assert state["account"]["telegram"] is False
    assert state["account"]["has_code"] is True
    assert who.post("/say", {"text": "Спал 8 часов"})[0] == 403

    assert who.post("/consent", {"agree": True})[0] == 200
    assert who.post("/say", {"text": "Спал 8 часов, пил кофе"})[0] == 200
    assert "кофе" in _texts(who.state()).lower()

    uid = service.accounts.session_user(who.sid)
    user = service.accounts.user(uid)
    assert uid < 0 and user["tg_id"] is None       # номер не из Telegram


def test_code_opens_the_same_diary_on_another_device(service):
    first, code = newcomer(service)
    first.post("/say", {"text": "Пил кофе вечером"})
    # человек переписал код как попало: строчными, пробелами, без тире
    sloppy = code.lower().replace("-", " ")
    status, second, body = by_code(service, sloppy)
    assert status == 200, body
    assert second.user_id == first.user_id
    assert "кофе" in _texts(second.state()).lower()


def test_wrong_code_says_nothing_about_existence(service):
    newcomer(service)
    answers = set()
    for attempt in ("AAAA-BBBB-CCCC-DDDD", "2345-6789-ABCD-EFGH", "чушь", "", None, 12345):
        status, _, body = by_code(service, attempt)
        assert status == 401
        answers.add(json.loads(body)["error"])
    assert len(answers) == 1 and WRONG in answers.pop()


def test_code_brute_force_is_429_per_address(service):
    for _ in range(10):
        assert by_code(service, "AAAA-BBBB-CCCC-DDDD", ip="10.0.0.9")[0] == 401
    status, _, body = by_code(service, "AAAA-BBBB-CCCC-DDDD", ip="10.0.0.9")
    assert status == 429
    # даже верный код с этого адреса - подождать
    _, code = newcomer(service, ip="10.0.0.5")
    assert by_code(service, code, ip="10.0.0.9")[0] == 429
    # соседний адрес не наказан
    assert by_code(service, code, ip="10.0.0.10")[0] == 200


def test_code_brute_force_from_many_addresses_hits_the_common_ceiling(service, monkeypatch):
    monkeypatch.setattr(server, "CODE_TOTAL", server.limits.Limiter(5, 60))
    for n in range(5):
        assert by_code(service, "AAAA-BBBB-CCCC-DDDD", ip=f"10.1.0.{n}")[0] == 401
    assert by_code(service, "AAAA-BBBB-CCCC-DDDD", ip="10.1.0.99")[0] == 429


def test_new_code_kills_the_old_one(service):
    who, old = newcomer(service)
    status, _, body = who.post("/account/code", {})
    assert status == 200
    new = json.loads(body)["code"]
    assert CODE_SHAPE.fullmatch(new) and new != old
    assert by_code(service, old)[0] == 401
    status, again, _ = by_code(service, new)
    assert status == 200 and again.user_id == who.user_id


def test_code_needs_session_and_csrf(service):
    assert _json_post(service, "/account/code", {})[0] == 401
    who, _ = newcomer(service)
    status, _, _ = service.request("POST", "/account/code", {}, {
        "Origin": ORIGIN, "Content-Type": "application/json",
        "Cookie": who.headers(csrf=False)["Cookie"]})
    assert status == 403


def test_only_salted_slow_hash_of_the_code_is_stored(service):
    _, first = newcomer(service, ip="10.0.0.1")
    _, second = newcomer(service, ip="10.0.0.2")
    raw = service.store.storage.path.read_bytes()
    for code in (first, second):
        assert code.encode() not in raw
        assert code.replace("-", "").encode() not in raw
    with sqlite3.connect(service.store.storage.path) as db:
        rows = db.execute("SELECT code_salt, code_hash, code_index FROM users").fetchall()
    assert len(rows) == 2
    (salt1, hash1, index1), (salt2, hash2, index2) = rows
    assert salt1 != salt2 and hash1 != hash2
    assert len(hash1) == 64 and len(index1) == 4       # scrypt 32 байта и 16 бит отпечатка


def test_two_site_users_are_isolated(service):
    alice, alice_code = newcomer(service, ip="10.0.0.1")
    bob, bob_code = newcomer(service, ip="10.0.0.2")
    assert alice.user_id != bob.user_id
    alice.post("/say", {"text": "Пил кофе вечером"})
    bob.post("/say", {"text": "Гулял в парке"})
    assert "кофе" not in _texts(bob.state()).lower()
    assert "парк" not in _texts(alice.state()).lower()
    # код Алисы открывает дневник Алисы, а не Боба
    _, again, _ = by_code(service, alice_code)
    assert again.user_id == alice.user_id
    assert "парк" not in _texts(again.state()).lower()
    # удаление Алисы не трогает Боба
    assert alice.post("/account/delete", {"confirm": "УДАЛИТЬ"})[0] == 200
    assert by_code(service, alice_code)[0] == 401
    assert by_code(service, bob_code)[0] == 200
    assert "парк" in _texts(bob.state()).lower()


def test_account_creation_is_limited_per_address(service):
    for _ in range(5):
        assert _json_post(service, "/auth/new", {}, ip="10.2.0.1")[0] == 200
    status, headers, _ = _json_post(service, "/auth/new", {}, ip="10.2.0.1")
    assert status == 429 and "Set-Cookie" not in headers
    assert service.accounts.count() == 5
    assert _json_post(service, "/auth/new", {}, ip="10.2.0.2")[0] == 200


def test_new_account_needs_our_page(service):
    # чужой сайт не заведёт человеку дневник и не подсунет свою сессию
    status, _, _ = service.request("POST", "/auth/new", {}, {
        "Origin": "https://evil.test", "Content-Type": "application/json"})
    assert status == 403
    assert service.accounts.count() == 0


def test_site_accounts_without_consent_are_swept_after_a_week(service):
    newcomer(service, consent=False)
    kept, _ = newcomer(service, ip="10.0.0.2")
    assert server.sweep_idle() == 0
    assert server.sweep_idle(now=time.time() + 8 * 24 * 60 * 60) == 1
    assert service.accounts.count() == 1
    assert service.accounts.user(kept.user_id) is not None


def test_telegram_user_can_get_a_code_too(service):
    who = service.person(101, "user101")
    assert who.state()["account"]["has_code"] is False
    status, _, body = who.post("/account/code", {})
    assert status == 200
    status, again, _ = by_code(service, json.loads(body)["code"])
    assert status == 200 and again.user_id == 101


# ── бот необязателен ──────────────────────────────────────────────────────

def test_server_starts_without_telegram_token(monkeypatch):
    monkeypatch.setattr(server, "PUBLIC_HOST", "mira.test")
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    server._check_server_settings()                  # не SystemExit
    monkeypatch.setenv("TELEGRAM_TOKEN", "не токен")
    with pytest.raises(SystemExit):
        server._check_server_settings()


def test_without_bot_the_page_hides_telegram(service):
    status, _, page = service.request("GET", "/")
    assert status == 200
    assert "Начать без Telegram" in page
    assert "const TG = '' === '1'" in page
    # и отказ без входа не отправляет к боту, которого нет
    status, _, body = service.request("GET", "/state")
    assert status == 401 and "бот" not in json.loads(body)["error"]
    who, _ = newcomer(service)
    assert who.state()["account"]["bot_on"] is False
    assert who.post("/account/telegram", {})[0] == 404


# ── подключение Telegram к дневнику с сайта ──────────────────────────────

@pytest.fixture
def bot(service, monkeypatch):
    bot = TelegramBot(token=FAKE_TOKEN, store=service.store, parser=dialog.Parser(),
                      accounts=service.accounts, public_url=PUBLIC)
    bot._call = FakeTelegram()
    monkeypatch.setattr(server, "SERVICE_BOT", bot)
    monkeypatch.setattr(server, "BOT_NAME", "mira_test_bot")
    return bot


def _link_token(who) -> str:
    status, _, body = who.post("/account/telegram", {})
    assert status == 200, body
    link = json.loads(body)["link"]
    assert link.startswith("https://t.me/mira_test_bot?start=")
    token = link.split("start=", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token)   # предел Telegram для start
    return token


def test_telegram_attaches_to_the_existing_site_user(service, bot):
    who, _ = newcomer(service)
    token = _link_token(who)
    bot.process(_update(f"/start {token}", user_id=555))
    assert "подключён" in bot._call.sent(555)[-1]["text"]
    # новый человек не заведён, чат привязан к тому, кто нажал кнопку
    assert service.accounts.count() == 1
    assert service.accounts.by_telegram(555) == who.user_id
    assert service.store.linked_chats(user_key(who.user_id)) == [555]
    account = who.state()["account"]
    assert account["telegram"] is True and account["username"] == "user555"
    # запись из бота видна на странице того же человека
    bot.process(_update("Пил кофе вечером", user_id=555, update_id=2))
    assert "кофе" in _texts(who.state()).lower()
    # /start без токена - тот же человек, а не новый
    bot.process(_update("/start", user_id=555, update_id=3))
    assert service.accounts.count() == 1
    # уже подключён - вторую ссылку не дают
    assert who.post("/account/telegram", {})[0] == 409


def test_link_token_works_once(service, bot):
    who, _ = newcomer(service)
    token = _link_token(who)
    bot.process(_update(f"/start {token}", user_id=555))
    bot.process(_update(f"/start {token}", user_id=556, update_id=2))
    assert "не сработала" in bot._call.sent(556)[-1]["text"]
    assert service.accounts.by_telegram(556) is None
    assert service.accounts.by_telegram(555) == who.user_id


def test_link_token_expires(service, bot, monkeypatch):
    who, _ = newcomer(service)
    token = _link_token(who)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 11 * 60)
    bot.process(_update(f"/start {token}", user_id=555))
    monkeypatch.setattr(time, "time", real)
    assert service.accounts.by_telegram(555) is None


def test_link_does_not_merge_with_another_telegram_diary(service, bot):
    bot.process(_update("/start", user_id=777))          # у 777 уже свой дневник
    who, _ = newcomer(service)
    token = _link_token(who)
    bot.process(_update(f"/start {token}", user_id=777, update_id=2))
    assert "другой дневник" in bot._call.sent(777)[-1]["text"]
    assert service.accounts.by_telegram(777) == 777
    assert service.accounts.user(who.user_id)["tg_id"] is None


def test_linked_user_is_erased_whole_from_the_bot(service, bot):
    who, code = newcomer(service)
    bot.process(_update(f"/start {_link_token(who)}", user_id=555))
    bot.process(_update("/reminder 21:00", user_id=555, update_id=2))
    bot.process(_update("/delete", user_id=555, update_id=3))
    bot.process(_update(ERASE_WORD, user_id=555, update_id=4))
    assert service.accounts.user(who.user_id) is None
    assert 555 not in bot.reminders.chats()
    with sqlite3.connect(service.store.storage.path) as db:
        assert db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
    assert by_code(service, code)[0] == 401


def test_normal_code_takes_only_full_codes():
    assert normal_code("abcd efgh jkmn pqrs") == "ABCD-EFGH-JKMN-PQRS"
    assert normal_code("ABCD-EFGH-JKMN-PQR") == ""
    assert normal_code("x" * 1000) == ""
    assert normal_code(None) == ""
