"""
Режим server без Telegram: «Начать без Telegram», личный код, перебор кода,
смена кода, привязка Telegram к уже заведённому на сайте дневнику и запуск
сервиса без TELEGRAM_TOKEN.

Сервер и браузер - те же, что в test_service.py, бот с подменённым API - тот
же, что в test_service_bot.py.
"""
import hashlib
from pathlib import Path
import json
import re
import sqlite3
import threading
import time
from datetime import datetime

import pytest

import web.server as server
from bot.telegram import ERASE_WORD, TelegramBot
import wam.accounts as accounts_module
from wam import dialog
from wam.accounts import CODE_ALPHABET, Accounts, normal_code, user_key
from wam.diary import DiaryStore

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


def test_site_accounts_without_consent_are_swept_after_a_day(service):
    newcomer(service, consent=False)
    kept, _ = newcomer(service, ip="10.0.0.2")
    service.accounts.enroll(101, "user101")           # из бота, тоже без согласия
    assert server.sweep_idle() == 0
    assert server.sweep_idle(now=time.time() + 25 * 60 * 60) == 1
    assert service.accounts.count() == 2               # из бота ждёт неделю
    assert service.accounts.user(kept.user_id) is not None
    assert server.sweep_idle(now=time.time() + 8 * 24 * 60 * 60) == 1
    assert service.accounts.user(101) is None


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
    status, headers, body = who.post("/account/telegram", {})
    assert status == 200, body
    assert headers["Cache-Control"] == "no-store"
    link = json.loads(body)["link"]
    assert link.startswith("https://t.me/mira_test_bot?start=")
    token = link.split("start=", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", token)   # предел Telegram для start
    return token


def _second_token(bot, chat_id) -> str:
    """Ответная ссылка, которую бот прислал в чат на /start <токен>."""
    text = bot._call.sent(chat_id)[-1]["text"]
    link = next(word for word in text.split() if word.startswith(PUBLIC + "/link#t="))
    return link.split("#t=", 1)[1]


def _confirm(who, token):
    return who.post("/account/telegram/confirm", {"token": token})


def test_telegram_is_linked_only_after_confirmation_in_the_same_browser(service, bot):
    who, _ = newcomer(service)
    bot.process(_update(f"/start {_link_token(who)}", user_id=555))
    second = _second_token(bot, 555)
    # бот только предложил чат - ничего не привязано
    assert service.accounts.by_telegram(555) is None
    assert service.accounts.user(who.user_id)["tg_id"] is None

    status, _, body = _confirm(who, second)
    assert status == 200, body
    assert service.accounts.count() == 1
    assert service.accounts.by_telegram(555) == who.user_id
    assert service.store.linked_chats(user_key(who.user_id)) == [555]
    assert "подключён" in bot._call.sent(555)[-1]["text"]
    account = who.state()["account"]
    assert account["telegram"] is True and account["username"] == "user555"
    # /start без токена - тот же человек, а не новый; вторую ссылку не дают
    bot.process(_update("/start", user_id=555, update_id=3))
    assert service.accounts.count() == 1
    assert who.post("/account/telegram", {})[0] == 409


def test_linked_chat_writes_only_after_consent_in_the_chat(service, bot):
    who, _ = newcomer(service)                     # согласие дано на сайте
    bot.process(_update(f"/start {_link_token(who)}", user_id=555))
    assert _confirm(who, _second_token(bot, 555))[0] == 200
    bot.process(_update("Пил кофе вечером", user_id=555, update_id=2))
    assert "согласие" in bot._call.sent(555)[-1]["text"].lower()
    assert "кофе" not in _texts(who.state()).lower()
    # согласие нажато в самом чате - теперь записывает
    bot.process({"update_id": 3, "callback_query": {
        "id": "q", "data": "consent", "from": {"id": 555}}})
    bot.process(_update("Пил кофе вечером", user_id=555, update_id=4))
    assert "кофе" in _texts(who.state()).lower()


def test_foreign_link_does_not_hand_the_victim_chat_to_the_attacker(service, bot):
    attacker, attacker_code = newcomer(service, ip="10.0.0.1")
    token = _link_token(attacker)
    # жертва нажала подсунутую ссылку: бот прислал ей вторую ссылку
    bot.process(_update(f"/start {token}", user_id=555))
    second = _second_token(bot, 555)
    # жертва открыла её в своём браузере - сессия не та
    victim, _ = newcomer(service, ip="10.0.0.3")
    assert _confirm(victim, second)[0] == 400
    assert service.accounts.by_telegram(555) is None
    # даже вторая сессия самого атакующего не подтвердит (и токен уже погашен)
    bot.process(_update(f"/start {_link_token(attacker)}", user_id=555, update_id=2))
    second = _second_token(bot, 555)
    _, other_device, _ = by_code(service, attacker_code)
    assert _confirm(other_device, second)[0] == 400
    assert _confirm(attacker, second)[0] == 400        # одноразовый
    assert service.accounts.by_telegram(555) is None
    # записи жертвы в дневник атакующего не идут
    bot.process(_update("Пил кофе вечером", user_id=555, update_id=3))
    assert "кофе" not in _texts(attacker.state()).lower()


def test_link_tokens_work_once(service, bot):
    who, _ = newcomer(service)
    token = _link_token(who)
    bot.process(_update(f"/start {token}", user_id=555))
    bot.process(_update(f"/start {token}", user_id=556, update_id=2))
    assert "не сработала" in bot._call.sent(556)[-1]["text"]
    second = _second_token(bot, 555)
    assert _confirm(who, second)[0] == 200
    assert _confirm(who, second)[0] in (400, 409)
    assert service.accounts.by_telegram(556) is None


def test_link_tokens_expire(service, bot, monkeypatch):
    who, _ = newcomer(service)
    token = _link_token(who)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 11 * 60)
    bot.process(_update(f"/start {token}", user_id=555))
    monkeypatch.setattr(time, "time", real)
    assert "не сработала" in bot._call.sent(555)[-1]["text"]
    bot.process(_update(f"/start {_link_token(who)}", user_id=555, update_id=2))
    second = _second_token(bot, 555)
    monkeypatch.setattr(time, "time", lambda: real() + 11 * 60)
    status = _confirm(who, second)[0]
    monkeypatch.setattr(time, "time", real)
    assert status == 400 and service.accounts.by_telegram(555) is None


def test_link_does_not_merge_with_another_telegram_diary(service, bot):
    bot.process(_update("/start", user_id=777))          # у 777 уже свой дневник
    who, _ = newcomer(service)
    bot.process(_update(f"/start {_link_token(who)}", user_id=777, update_id=2))
    assert "другой дневник" in bot._call.sent(777)[-1]["text"]
    assert service.accounts.by_telegram(777) == 777
    assert service.accounts.user(who.user_id)["tg_id"] is None


def test_linked_user_is_erased_whole_from_the_bot(service, bot):
    who, code = newcomer(service)
    bot.process(_update(f"/start {_link_token(who)}", user_id=555))
    assert _confirm(who, _second_token(bot, 555))[0] == 200
    bot.process({"update_id": 2, "callback_query": {
        "id": "q", "data": "consent", "from": {"id": 555}}})
    bot.process(_update("/reminder 21:00", user_id=555, update_id=3))
    bot.process(_update("/delete", user_id=555, update_id=4))
    bot.process(_update(ERASE_WORD, user_id=555, update_id=5))
    assert service.accounts.user(who.user_id) is None
    assert 555 not in bot.reminders.chats()
    with sqlite3.connect(service.store.storage.path) as db:
        assert db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
    assert by_code(service, code)[0] == 401


def test_deleted_chat_has_no_diary_and_no_reminders(service, bot):
    service.accounts.enroll(555, "user555")
    service.accounts.agree_in_chat(555)
    bot.reminders.remember(555)
    service.accounts.erase(555, service.store)        # удалили в обход бота
    assert bot._diary_of(555) is None
    assert "/start" in bot.summary(555)
    bot.remind(datetime.now().replace(hour=21, minute=30))
    assert 555 not in bot.reminders.chats()
    assert service.accounts.orphans() == []            # ничейного дневника не завели


# ── находки аудита ────────────────────────────────────────────────────────

def test_code_change_is_limited_per_person(service):
    who, _ = newcomer(service)
    for _ in range(5):
        assert who.post("/account/code", {})[0] == 200
    assert who.post("/account/code", {})[0] == 429


def test_code_change_closes_other_sessions_but_not_this_one(service):
    who, code = newcomer(service)
    _, other, _ = by_code(service, code)
    assert other.get("/state")[0] == 200
    status, headers, _ = who.post("/account/code", {})
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert other.get("/state")[0] == 401
    assert who.get("/state")[0] == 200


def test_new_account_answer_is_not_cached(service):
    status, headers, _ = _json_post(service, "/auth/new", {})
    assert status == 200 and headers["Cache-Control"] == "no-store"


def test_scrypt_busy_is_503_and_leaves_no_account(service, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()                                   # всё занято
    monkeypatch.setattr(accounts_module, "SCRYPT_SLOTS", slots)
    monkeypatch.setattr(accounts_module, "SCRYPT_WAIT", 0.05)
    status, headers, _ = _json_post(service, "/auth/new", {})
    assert status == 503 and headers.get("Retry-After") == "2"
    assert service.accounts.count() == 0
    assert by_code(service, "AAAA-BBBB-CCCC-DDDD")[0] == 503


def test_code_check_costs_one_scrypt_whether_or_not_it_matches(service, monkeypatch):
    who, code = newcomer(service)
    calls = []
    real = hashlib.scrypt
    monkeypatch.setattr(hashlib, "scrypt", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert service.accounts.redeem_code("AAAA-BBBB-CCCC-DDDD") is None
    assert len(calls) == 1
    assert service.accounts.redeem_code(code) == who.user_id
    assert len(calls) == 2
    service.accounts.issue_code(who.user_id)           # без лишней проверки повтора
    assert len(calls) == 3


def test_ipv6_is_limited_per_64_network(service):
    for n in range(10):
        assert by_code(service, "AAAA-BBBB-CCCC-DDDD", ip=f"2001:db8:1:2::{n + 1:x}")[0] == 401
    assert by_code(service, "AAAA-BBBB-CCCC-DDDD", ip="2001:db8:1:2:ffff::1")[0] == 429
    assert by_code(service, "AAAA-BBBB-CCCC-DDDD", ip="2001:db8:1:3::1")[0] == 401
    assert server._per_network("2001:db8:1:2::5") == "2001:db8:1:2::/64"
    assert server._per_network("::ffff:10.1.2.3") == "10.1.2.3"
    assert server._per_network("10.1.2.3") == "10.1.2.3"


def test_account_creation_has_a_common_ceiling(service, monkeypatch):
    monkeypatch.setattr(server, "CREATE_TOTAL", server.limits.Limiter(2, 3600))
    assert _json_post(service, "/auth/new", {}, ip="10.3.0.1")[0] == 200
    assert _json_post(service, "/auth/new", {}, ip="10.3.0.2")[0] == 200
    assert _json_post(service, "/auth/new", {}, ip="10.3.0.3")[0] == 429


def test_account_creation_stops_when_too_many_wait_for_consent(service, monkeypatch):
    monkeypatch.setattr(server, "MAX_WAITING_CONSENT", 1)
    newcomer(service, ip="10.4.0.1", consent=False)
    newcomer(service, ip="10.4.0.2", consent=False)
    assert _json_post(service, "/auth/new", {}, ip="10.4.0.3")[0] == 429
    assert service.accounts.count() == 2


def test_privacy_tells_about_addresses_and_voice(service, monkeypatch):
    monkeypatch.setattr(server, "PROXY_LOG_DAYS", 30)
    _, _, page = service.request("GET", "/privacy")
    assert "оперативной памяти" in page and "30 дней" in page
    assert "ничего, кроме" not in page
    assert "голос выключен" in page
    assert "{PROXY_LOG_DAYS}" not in page


def test_tg_id_migration_skips_taken_numbers_quietly(tmp_path, capsys):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, "
                   "username TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
                   "consent_at REAL, consent_version TEXT, active_at REAL, tg_id INTEGER)")
        db.execute("INSERT INTO users(user_id, created_at) VALUES(424242, 0)")
        db.execute("INSERT INTO users(user_id, created_at) VALUES(5, 0)")
        # человек с сайта уже подключил Telegram 424242
        db.execute("INSERT INTO users(user_id, created_at, tg_id) VALUES(-7, 0, 424242)")
    store = DiaryStore(path)
    accounts = Accounts(store.storage)
    assert accounts.by_telegram(424242) == -7
    assert accounts.user(424242)["tg_id"] is None
    assert accounts.user(5)["tg_id"] == 5
    out = capsys.readouterr().out
    assert "1" in out and "424242" not in out
    store.close()


def test_normal_code_takes_only_full_codes():
    assert normal_code("abcd efgh jkmn pqrs") == "ABCD-EFGH-JKMN-PQRS"
    assert normal_code("ABCD-EFGH-JKMN-PQR") == ""
    assert normal_code("x" * 1000) == ""
    assert normal_code(None) == ""


# ── кольцо и КБЖУ на сервере ──────────────────────────────────────────────
# Живой прогон 11.11.2026: ползунки кольца стояли на придуманных значениях, и
# «качество сна 4.1 из 10 (с кольца)» ложилось в настоящий дневник человека.

def test_ring_readings_do_not_reach_a_real_diary(service):
    who, _ = newcomer(service)
    status, _, body = who.post("/say", {"text": "Пил кофе вечером",
                                        "ring": {"sleep_score": 41, "stress_level": 72,
                                                 "steps": 3000}})
    assert status == 200, body
    text = _texts(who.state()).lower()
    assert "с кольца" not in text and "кофе" in text
    diary = service.store.get(user_key(who.user_id))
    with diary.lock:
        sources = {fact.source for record in diary.timeline.days for fact in record.facts}
    assert sources <= {"diary"}


def test_food_numbers_are_not_connected_on_the_server(service):
    who, _ = newcomer(service)
    status, _, body = who.post("/food", {"калории": 2000})
    assert status == 403 and "не подключено" in json.loads(body)["error"]


def test_page_shows_ring_and_food_as_not_connected(service):
    _, _, page = service.request("GET", "/")
    assert "const DEVICES = '' === '1'" in page


def test_server_mode_starts_with_ring_and_food_off(tmp_path):
    import os
    import subprocess
    import sys
    env = {**os.environ, "MIRA_MODE": "server", "MIRA_DB": str(tmp_path / "m.db"),
           "WAM_ENGINE": "правила"}
    env.pop("TELEGRAM_TOKEN", None)
    out = subprocess.run([sys.executable, "-c", "import web.server as s; print(s.DEVICES)"],
                         env=env, capture_output=True, text=True, cwd=Path(__file__).parent.parent)
    assert out.stdout.strip() == "False", out.stderr
