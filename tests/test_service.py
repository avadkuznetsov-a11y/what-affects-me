"""
Режим server: вход по ссылке, сессии, CSRF и главное - чужой дневник не видно.

Сервер поднимается настоящий, на свободном порту, но с подменённым режимом и
своей базой во временном каталоге. Запросы собираются руками через
http.client: так видно каждый заголовок - Host, Origin, Cookie, X-CSRF, - и
можно отправить ровно тот кривой запрос, от которого защищаемся.
"""
import http.client
import json
import re
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import web.server as server
import wam.accounts as accounts_module
from demo.generate import build
from wam import dialog, limits, llm, weather
from wam.accounts import COOKIE, Accounts, csrf_for, user_key
from wam.derive import derive_factors
from wam.diary import DiaryStore

HOST = "mira.test"
ORIGIN = f"https://{HOST}"


class Service:
    """Поднятый сервер и всё, что тесту нужно знать о нём."""

    def __init__(self, port: int, store: DiaryStore, accounts: Accounts) -> None:
        self.port = port
        self.store = store
        self.accounts = accounts

    def request(self, method, path, body=None, headers=None):
        """Ответ как (код, заголовки, тело-строка)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        sent = {"Host": HOST}
        sent.update(headers or {})
        conn.request(method, path, body=data, headers=sent)
        answer = conn.getresponse()
        text = answer.read().decode(errors="replace")
        result = answer.status, dict(answer.getheaders()), text
        conn.close()
        return result

    def person(self, user_id: int, username: str = "", consent: bool = True) -> "Person":
        """Человек, который написал боту /start и вошёл по ссылке."""
        self.accounts.enroll(user_id, username)
        if consent:
            self.accounts.agree(user_id)
        who = Person(self, user_id)
        who.login(self.accounts.issue_login(user_id))
        return who


class Person:
    """Браузер одного человека: своя cookie и своя метка CSRF."""

    def __init__(self, service: Service, user_id: int) -> None:
        self.service = service
        self.user_id = user_id
        self.sid = ""

    def login(self, token):
        code, headers, body = self.service.request(
            "POST", "/auth/login", {"token": token},
            {"Origin": ORIGIN, "Content-Type": "application/json"})
        if code == 200:
            cookie = headers["Set-Cookie"]
            self.sid = cookie.split(";", 1)[0].split("=", 1)[1]
        return code, headers, body

    def headers(self, csrf=True):
        sent = {"Cookie": f"{COOKIE}={self.sid}"}
        if csrf:
            sent["X-CSRF"] = csrf_for(self.sid)
        return sent

    def get(self, path):
        return self.service.request("GET", path, headers=self.headers(csrf=False))

    def post(self, path, body=None, **extra):
        sent = {"Origin": ORIGIN, "Content-Type": "application/json"}
        sent.update(self.headers())
        sent.update(extra)
        return self.service.request("POST", path, {} if body is None else body, sent)

    def state(self) -> dict:
        code, _, body = self.get("/state")
        assert code == 200, body
        return json.loads(body)


@pytest.fixture
def service(monkeypatch, tmp_path):
    store = DiaryStore(tmp_path / "mira.db")
    accounts = Accounts(store.storage)
    monkeypatch.setattr(server, "MODE", "server")
    monkeypatch.setattr(server, "DEVICES", False)       # как в настоящем режиме server
    monkeypatch.setattr(server, "PUBLIC_HOST", HOST)
    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "ACCOUNTS", accounts)
    monkeypatch.setattr(server, "PARSER", dialog.Parser())
    monkeypatch.setattr(server, "SERVICE_BOT", None)
    monkeypatch.setattr(server, "LOGIN_LIMIT", limits.Limiter(10, 60))
    monkeypatch.setattr(server, "WEATHER_LIMIT", limits.Limiter(10, 3600))
    monkeypatch.setattr(server, "SUMMARY_LIMIT", limits.Limiter(10, 60))
    monkeypatch.setattr(server, "FOOD_LIMIT", limits.Limiter(10, 60))
    monkeypatch.setattr(server, "CITY_LIMIT", limits.Limiter(10, 3600))
    monkeypatch.setattr(server, "CODE_LIMIT", limits.Limiter(10, 60))
    monkeypatch.setattr(server, "CODE_TOTAL", limits.Limiter(100, 60))
    monkeypatch.setattr(server, "CREATE_LIMIT", limits.Limiter(5, 3600))
    monkeypatch.setattr(server, "CREATE_TOTAL", limits.Limiter(100, 3600))
    monkeypatch.setattr(server, "CODE_CHANGE_LIMIT", limits.Limiter(5, 3600))
    monkeypatch.setattr(server, "BOT_NAME", "")
    # Город проверяется геокодингом, погода - запросом в сеть. В тестах сети
    # нет: город «находится» всегда, погоды нет никогда.
    monkeypatch.setattr(weather, "coords", lambda city: (55.75, 37.62))
    monkeypatch.setattr(weather, "readings", lambda city: {})
    server._CACHE[server.DEFAULT_DAYS] = (derive_factors(build(days=21)), [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield Service(httpd.server_address[1], store, accounts)
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def devices_on(monkeypatch):
    """Кольцо и КБЖУ включены, как в режиме local: проверяем сам их путь."""
    monkeypatch.setattr(server, "DEVICES", True)


def _texts(state: dict) -> str:
    return " ".join(m["text"] for m in state["messages"])


# ── без входа ─────────────────────────────────────────────────────────────

PRIVATE_GET = ("/state", "/export", "/export?format=csv", "/nothing-here")
PRIVATE_POST = ("/say", "/summary", "/reset", "/city", "/weather", "/food",
                "/auth/logout", "/telegram/connect", "/nothing-here")


def test_page_and_login_page_are_open(service):
    for path in ("/", "/login"):
        code, _, html = service.request("GET", path)
        assert code == 200 and 'id="feed"' in html


@pytest.mark.parametrize("path", PRIVATE_GET)
def test_without_session_every_get_is_401(service, path):
    assert service.request("GET", path)[0] == 401
    # Выдуманная cookie - то же самое, что никакой
    assert service.request("GET", path, headers={"Cookie": f"{COOKIE}=made-up"})[0] == 401


@pytest.mark.parametrize("path", PRIVATE_POST)
def test_without_session_every_post_is_401(service, path):
    code, _, _ = service.request("POST", path, {"text": "пил кофе"},
                                 {"Origin": ORIGIN, "Content-Type": "application/json"})
    assert code == 401


def test_page_from_local_mode_endpoints_is_gone(service):
    """Панели бота со страницы в режиме server нет: и с сессией это 404."""
    alice = service.person(1)
    for path in ("/telegram/connect", "/telegram/disconnect", "/telegram/code",
                 "/telegram/unlink"):
        assert alice.post(path, {"token": "x"})[0] == 404


# ── вход по ссылке ────────────────────────────────────────────────────────

def test_login_sets_a_strict_cookie_and_state_works(service):
    alice = service.person(1, "alice")
    assert alice.sid
    code, headers, _ = alice.get("/state")
    assert code == 200
    cookie = headers["Set-Cookie"]
    for part in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert part in cookie
    assert cookie.startswith(COOKIE + "=")
    state = json.loads(alice.get("/state")[2])
    assert state["mode"] == "server"
    assert state["csrf"] == csrf_for(alice.sid)
    assert state["account"]["username"] == "alice"
    assert "telegram" not in state


def test_login_link_works_once(service):
    service.accounts.enroll(1)
    token = service.accounts.issue_login(1)
    first = Person(service, 1)
    assert first.login(token)[0] == 200
    second = Person(service, 1)
    assert second.login(token)[0] == 401
    assert not second.sid


def test_login_link_expires(service, monkeypatch):
    service.accounts.enroll(1)
    monkeypatch.setattr(accounts_module, "LOGIN_LIFETIME", -1)
    token = service.accounts.issue_login(1)
    assert Person(service, 1).login(token)[0] == 401


def test_new_login_link_cancels_the_old_one(service):
    service.accounts.enroll(1)
    old = service.accounts.issue_login(1)
    new = service.accounts.issue_login(1)
    assert Person(service, 1).login(old)[0] == 401
    assert Person(service, 1).login(new)[0] == 200


def test_nonsense_tokens_are_refused(service):
    for token in (None, 123, "", "x" * 5000, ["a"], "выдумка"):
        assert Person(service, 1).login(token)[0] == 401


def test_only_hash_of_secrets_is_stored(service):
    alice = service.person(1)
    token = service.accounts.issue_login(1)
    with service.store.storage.atomic() as db:
        rows = db.execute("SELECT sid_hash FROM sessions").fetchall()
        rows += db.execute("SELECT token_hash FROM login_tokens").fetchall()
    stored = " ".join(row[0] for row in rows)
    assert alice.sid not in stored and token not in stored


def test_session_expires(service, monkeypatch):
    alice = service.person(1)
    monkeypatch.setattr(accounts_module, "SESSION_LIFETIME", -1)
    assert alice.get("/state")[0] == 401


def test_logout_closes_the_session(service):
    alice = service.person(1)
    code, headers, _ = alice.post("/auth/logout")
    assert code == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert alice.get("/state")[0] == 401


def test_deleted_user_cannot_log_in_with_old_link(service):
    service.accounts.enroll(1)
    token = service.accounts.issue_login(1)
    with service.store.storage.atomic() as db:
        db.execute("DELETE FROM users WHERE user_id=1")
    assert Person(service, 1).login(token)[0] == 401


# ── чужие сайты и CSRF ────────────────────────────────────────────────────

def test_foreign_origin_is_403(service):
    alice = service.person(1)
    for origin in ("https://evil.test", f"http://{HOST}", f"https://{HOST}:8443", "null"):
        assert alice.post("/say", {"text": "пил кофе"}, Origin=origin)[0] == 403
    # и вход по чужому сайту - тоже
    code, _, _ = service.request("POST", "/auth/login", {"token": "x"},
                                 {"Origin": "https://evil.test",
                                  "Content-Type": "application/json"})
    assert code == 403


def test_post_without_origin_is_403(service):
    alice = service.person(1)
    sent = {"Content-Type": "application/json"}
    sent.update(alice.headers())
    assert service.request("POST", "/say", {"text": "пил кофе"}, sent)[0] == 403


def test_foreign_host_is_403(service):
    alice = service.person(1)
    assert alice.post("/say", {"text": "x"}, Host="127.0.0.1")[0] == 403
    assert service.request("GET", "/", headers={"Host": "evil.test"})[0] == 403


def test_cross_site_fetch_is_403(service):
    alice = service.person(1)
    sent = alice.headers(csrf=False)
    sent["Sec-Fetch-Site"] = "cross-site"
    assert service.request("GET", "/export", headers=sent)[0] == 403


def test_post_without_csrf_is_403(service):
    alice = service.person(1)
    assert alice.post("/say", {"text": "пил кофе"}, **{"X-CSRF": ""})[0] == 403
    assert alice.post("/say", {"text": "пил кофе"}, **{"X-CSRF": "made-up"})[0] == 403
    # метка другого человека к этой сессии не подходит
    bob = service.person(2)
    assert alice.post("/say", {"text": "пил кофе"},
                      **{"X-CSRF": csrf_for(bob.sid)})[0] == 403
    assert "кофе" not in _texts(alice.state())


def test_post_without_json_type_is_refused(service):
    alice = service.person(1)
    code, _, _ = alice.post("/say", {"text": "пил кофе"}, **{"Content-Type": "text/plain"})
    assert code == 415
    code, _, _ = alice.post("/say", {"text": "пил кофе"},
                            **{"Content-Type": "application/x-www-form-urlencoded"})
    assert code == 415


# ── два человека: каждый видит только своё ────────────────────────────────

def test_user_key_is_never_taken_before_the_login_check():
    handler = server.Handler.__new__(server.Handler)
    handler.uid = None
    original = server.MODE
    server.MODE = "server"
    try:
        with pytest.raises(RuntimeError):
            handler._user_key()
    finally:
        server.MODE = original


def test_say_and_state_are_separate(service):
    alice, bob = service.person(1), service.person(2)
    # Подложенный в тело чужой номер ничего не значит: ключ берётся из сессии
    assert alice.post("/say", {"text": "Пил кофе, с утра тревога",
                               "key": user_key(2), "user_id": 2})[0] == 200
    assert "кофе" in _texts(alice.state())
    assert "кофе" not in _texts(bob.state())
    assert service.store.get(user_key(1)).today().factor("кофе") == 1.0
    assert service.store.get(user_key(2)).today().factor("кофе") is None


def test_summary_goes_only_to_own_feed(service):
    alice, bob = service.person(1), service.person(2)
    assert alice.post("/summary", {"days": server.DEFAULT_DAYS})[0] == 200
    assert alice.state()["messages"]
    assert bob.state()["messages"] == []


def test_city_and_weather_are_separate(service, monkeypatch):
    alice, bob = service.person(1), service.person(2)
    assert alice.post("/city", {"city": "Москва"})[0] == 200
    assert alice.state()["city"] == "Москва"
    assert bob.state()["city"] == ""
    asked = []
    monkeypatch.setattr(weather, "readings", lambda city: asked.append(city) or {})
    code, _, body = bob.post("/weather")
    assert code == 200 and json.loads(body)["city"] == ""
    assert asked == []          # про чужой город Боба и не спрашивали
    code, _, body = alice.post("/weather")
    assert json.loads(body)["city"] == "Москва" and asked == ["Москва"]


def test_food_is_separate(service, devices_on):
    alice, bob = service.person(1), service.person(2)
    assert alice.post("/food", {"калории": 4000})[0] == 200
    assert service.store.get(user_key(1)).today().facts
    assert not service.store.get(user_key(2)).today().facts
    assert "КБЖУ" not in _texts(bob.state())


def test_export_is_separate(service):
    alice, bob = service.person(1), service.person(2)
    alice.post("/say", {"text": "Пил кофе, с утра тревога"})
    for kind in ("text", "csv"):
        assert "кофе" in alice.get(f"/export?format={kind}")[2]
        code, _, body = bob.get(f"/export?format={kind}")
        assert code == 200 and "кофе" not in body


def test_reset_touches_only_own_diary(service):
    alice, bob = service.person(1), service.person(2)
    alice.post("/say", {"text": "Пил кофе, с утра тревога"})
    bob.post("/say", {"text": "Пил кофе, с утра тревога"})
    assert bob.post("/reset")[0] == 200
    assert service.store.get(user_key(1)).today().factor("кофе") == 1.0
    assert "кофе" in _texts(alice.state())
    assert bob.state()["messages"] == []


def test_logout_closes_only_own_session(service):
    alice, bob = service.person(1), service.person(2)
    alice.post("/auth/logout")
    assert bob.get("/state")[0] == 200


# ── гостевая чистка ───────────────────────────────────────────────────────

def test_old_user_diary_is_not_swept_as_a_guest(tmp_path):
    """
    Непривязанные дневники tg:* хранилище выкидывает через неделю. Дневник
    человека сервиса u:* - не гостевой, и сколько бы в него ни писали в
    последний раз, он должен пережить перезапуск.
    """
    path = tmp_path / "mira.db"
    store = DiaryStore(path)
    for key in (user_key(7), "tg:8"):
        diary = store.get(key)
        diary.city = "Москва"
        diary.save()
    with store.storage.atomic() as db:
        db.execute("UPDATE diaries SET seen=?", (time.time() - 365 * 24 * 3600,))
    store.storage.close()

    again = DiaryStore(path)
    assert again.get(user_key(7)).city == "Москва"
    assert again.get("tg:8").city == ""          # гостевой ушёл, как и раньше
    again._guests[user_key(7)] = 0.0             # даже если попал в гости по ошибке
    again._forget_guests()
    assert again.get(user_key(7)).city == "Москва"


# ── согласие ──────────────────────────────────────────────────────────────

def test_without_consent_the_diary_is_closed(service):
    alice = service.person(1, consent=False)
    state = alice.state()
    assert state["consent_needed"] and "messages" not in state and "city" not in state
    for path in ("/say", "/summary", "/reset", "/city", "/weather", "/food"):
        code, _, body = alice.post(path, {"text": "Пил кофе"})
        assert code == 403 and json.loads(body)["consent"], path
    # Свои записи забрать можно и без согласия - доступ к своим данным
    assert alice.get("/export")[0] == 200
    assert not service.store.get(user_key(1)).timeline.days


def test_consent_opens_the_diary(service):
    alice = service.person(1, consent=False)
    assert alice.post("/consent", {"agree": False})[0] == 400
    assert alice.post("/consent", {"agree": True})[0] == 200
    assert service.accounts.has_consent(1)
    assert alice.post("/say", {"text": "Пил кофе, с утра тревога"})[0] == 200
    assert "кофе" in _texts(alice.state())


def test_consent_is_own_and_needs_csrf(service):
    alice, bob = service.person(1, consent=False), service.person(2, consent=False)
    assert alice.post("/consent", {"agree": True}, **{"X-CSRF": ""})[0] == 403
    alice.post("/consent", {"agree": True})
    assert service.accounts.has_consent(1) and not service.accounts.has_consent(2)
    assert bob.post("/say", {"text": "Пил кофе"})[0] == 403


def test_new_consent_text_asks_again(service, monkeypatch):
    alice = service.person(1)
    assert "messages" in alice.state()
    monkeypatch.setattr(accounts_module, "CONSENT_VERSION", "новая версия")
    assert alice.state()["consent_needed"]


def test_privacy_page_is_open_and_marked_as_draft(service, monkeypatch):
    code, _, page = service.request("GET", "/privacy")
    assert code == 200 and "черновик" in page.lower()
    assert "[оператор уточняется]" in page and "{OPERATOR}" not in page
    assert accounts_module.CONSENT_VERSION in page
    assert "Telegram" in page and "365" in page
    monkeypatch.setattr(server, "OPERATOR", "ООО <Проба>")
    page = service.request("GET", "/privacy")[2]
    assert "ООО &lt;Проба&gt;" in page


# ── удаление ──────────────────────────────────────────────────────────────

def _left(store, user_id) -> int:
    """Сколько строк о человеке осталось во всех таблицах базы вместе."""
    key = user_key(user_id)
    queries = (
        ("SELECT COUNT(*) FROM users WHERE user_id=?", user_id),
        ("SELECT COUNT(*) FROM sessions WHERE user_id=?", user_id),
        ("SELECT COUNT(*) FROM login_tokens WHERE user_id=?", user_id),
        ("SELECT COUNT(*) FROM reminders WHERE chat_id=?", user_id),
        ("SELECT COUNT(*) FROM chats WHERE diary=?", key),
        ("SELECT COUNT(*) FROM diaries WHERE diary=?", key),
        ("SELECT COUNT(*) FROM days WHERE diary=?", key),
        ("SELECT COUNT(*) FROM facts WHERE diary=?", key),
    )
    with store.storage.atomic() as db:
        return sum(db.execute(sql, (arg,)).fetchone()[0] for sql, arg in queries)


def _fill(service, who):
    who.post("/say", {"text": "Пил кофе, с утра тревога"})
    who.post("/city", {"city": "Москва"})
    service.store.attach(who.user_id, user_key(who.user_id))
    service.accounts.issue_login(who.user_id)
    with service.store.storage.atomic() as db:
        db.execute("INSERT INTO reminders(chat_id, at) VALUES(?, 1200)", (who.user_id,))


def test_delete_needs_the_word(service):
    alice = service.person(1)
    assert alice.post("/account/delete", {"confirm": "да"})[0] == 400
    assert alice.post("/account/delete", {})[0] == 400
    assert service.accounts.user(1) is not None


def test_delete_wipes_every_table_and_only_own(service):
    alice, bob = service.person(1), service.person(2)
    _fill(service, alice)
    _fill(service, bob)
    assert _left(service.store, 1) >= 8
    bob_before = _left(service.store, 2)

    code, headers, _ = alice.post("/account/delete", {"confirm": "УДАЛИТЬ"})
    assert code == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert _left(service.store, 1) == 0
    assert alice.get("/state")[0] == 401
    assert _left(service.store, 2) == bob_before
    assert "кофе" in _texts(bob.state())


def test_delete_works_without_consent(service):
    alice = service.person(1, consent=False)
    assert alice.post("/account/delete", {"confirm": "УДАЛИТЬ"})[0] == 200
    assert service.accounts.user(1) is None


# ── срок хранения ─────────────────────────────────────────────────────────

def test_idle_accounts_are_swept_whole(service):
    alice, bob, carol = service.person(1), service.person(2), service.person(3)
    _fill(service, alice)
    _fill(service, bob)
    year = 366 * 24 * 3600
    with service.store.storage.atomic() as db:
        db.execute("UPDATE users SET created_at=?, active_at=?", (0, 0))
        db.execute("UPDATE diaries SET seen=0")
        # Боб не заходил год, но вчера писал в дневник через бота
        db.execute("UPDATE diaries SET seen=? WHERE diary=?",
                   (time.time() - 24 * 3600, user_key(2)))
    assert sorted(service.accounts.idle(365)) == [1, 3]
    assert server.sweep_idle() == 2
    assert _left(service.store, 1) == 0 and _left(service.store, 3) == 0
    assert service.accounts.user(2) is not None
    assert "кофе" in _texts(bob.state())
    # заход на страницу продлевает срок
    assert service.accounts.idle(365, now=time.time() + year) == [2]


def test_login_counts_as_activity(service):
    service.person(1)
    with service.store.storage.atomic() as db:
        db.execute("UPDATE users SET created_at=0, active_at=0")
    assert service.accounts.idle(365) == [1]
    service.accounts.open_session(1)
    assert service.accounts.idle(365) == []


# ── лимиты ────────────────────────────────────────────────────────────────

def test_say_is_limited_per_minute(service):
    alice, bob = service.person(1), service.person(2)
    codes = [alice.post("/say", {"text": "Пил кофе"})[0] for _ in range(21)]
    assert codes[:20] == [200] * 20
    code, headers, body = alice.post("/say", {"text": "Пил кофе"})
    assert code == 429 and headers["Retry-After"] == "60"
    assert json.loads(body)["error"]
    assert bob.post("/say", {"text": "Пил кофе"})[0] == 200      # у другого свой счёт


def test_say_is_limited_per_day(service, monkeypatch):
    monkeypatch.setattr(limits, "SAY_PER_DAY", limits.Limiter(3, 24 * 3600))
    alice = service.person(1)
    codes = [alice.post("/say", {"text": "Пил кофе"})[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_daily_limit_comes_from_environment(monkeypatch):
    monkeypatch.setenv("MIRA_DAILY_LIMIT", "42")
    assert limits._daily_limit() == 42
    monkeypatch.setenv("MIRA_DAILY_LIMIT", "чепуха")
    assert limits._daily_limit() == 300


def test_weather_is_limited_per_hour(service):
    alice = service.person(1)
    codes = [alice.post("/weather")[0] for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_login_is_limited_per_address(service):
    def attempt(ip=None):
        sent = {"Origin": ORIGIN, "Content-Type": "application/json"}
        if ip:
            sent["X-Real-IP"] = ip
        return service.request("POST", "/auth/login", {"token": "made-up"}, sent)[0]

    codes = [attempt("203.0.113.5") for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429
    # другой адрес за тем же nginx-proxy - свой счёт
    assert attempt("203.0.113.6") == 401


# ── модель ────────────────────────────────────────────────────────────────

def test_model_error_text_is_not_shown_to_everyone(service):
    alice = service.person(1)
    server.PARSER.last_error = "could not convert string to float: 'болит сердце'"
    try:
        state = alice.state()
    finally:
        server.PARSER.last_error = ""
    assert state["model_error"] and "сердце" not in state["model_error"]


class _Answer:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_yandexgpt_asks_not_to_log_and_uses_new_model(monkeypatch):
    import urllib.request

    sent = []

    def fake_urlopen(request, timeout=None, context=None):
        sent.append(request)
        return _Answer(json.dumps({"result": {"alternatives": [
            {"message": {"text": "{}"}}]}}).encode())

    monkeypatch.setenv("YANDEX_API_KEY", "key-for-test")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "folder-for-test")
    monkeypatch.delenv("YANDEX_MODEL", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert llm.yandexgpt_complete()("разбери") == "{}"
    request = sent[0]
    assert request.get_header("X-data-logging-enabled") == "false"
    assert json.loads(request.data)["modelUri"] == "gpt://folder-for-test/yandexgpt-5.1/latest"

    monkeypatch.setenv("YANDEX_MODEL", "yandexgpt/rc")
    llm.yandexgpt_complete()("разбери")
    assert json.loads(sent[1].data)["modelUri"] == "gpt://folder-for-test/yandexgpt/rc"


def test_server_mode_allows_only_yandexgpt(monkeypatch):
    for name in ("GIGACHAT_TOKEN", "YANDEX_API_KEY", "YANDEX_FOLDER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key-for-test")
    monkeypatch.delenv("WAM_ENGINE", raising=False)
    assert llm.available_engine(only=("YandexGPT",))[0] == "правила"
    monkeypatch.setenv("WAM_ENGINE", "claude")
    with pytest.raises(RuntimeError):
        llm.available_engine(only=("YandexGPT",))
    # назван, но ключа нет - сервис работает на правилах, а не падает
    monkeypatch.setenv("WAM_ENGINE", "yandex")
    assert llm.available_engine(only=("YandexGPT",))[0] == "правила"
    monkeypatch.setenv("YANDEX_API_KEY", "key-for-test")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "folder-for-test")
    assert llm.available_engine(only=("YandexGPT",))[0] == "YandexGPT"


# ── выкладка: проверка живости, заголовки, остановка ──────────────────────

def test_healthz_is_open_and_says_only_ok(service):
    """
    Docker стучится изнутри контейнера на 127.0.0.1 - без нашего Host, без
    cookie. Ответ - одно слово, никаких данных.
    """
    for host in (HOST, "127.0.0.1:8765"):
        code, headers, body = service.request("GET", "/healthz", headers={"Host": host})
        assert (code, body) == (200, "ok")
        assert "Set-Cookie" not in headers


def _pages_and_answers(service):
    """Ответы всех видов: страница, политика, данные, отказы, 404, healthz."""
    alice = service.person(1)
    alice.post("/say", {"text": "Пил кофе, с утра тревога"})
    return {
        "/": service.request("GET", "/"),
        "/privacy": service.request("GET", "/privacy"),
        "/healthz": service.request("GET", "/healthz"),
        "/state": alice.get("/state"),
        "/export": alice.get("/export?format=csv"),
        "/say": alice.post("/say", {"text": "Спал 7 часов"}),
        "401": service.request("GET", "/state"),
        "403": service.request("GET", "/", headers={"Host": "evil.test"}),
        "404": alice.get("/nothing-here"),
    }


def test_every_answer_carries_security_headers(service):
    for name, (code, headers, _) in _pages_and_answers(service).items():
        assert headers.get("X-Frame-Options") == "DENY", name
        assert headers.get("X-Content-Type-Options") == "nosniff", name
        assert headers.get("Referrer-Policy") == "no-referrer", name
        assert headers.get("Strict-Transport-Security", "").startswith("max-age="), name
        # Данные - чужой дневник, сессия, метка CSRF - не оседают ни в кэше
        # браузера, ни на прокси
        assert headers.get("Cache-Control") == "no-store", name
        policy = headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy, name


def test_policy_lets_in_only_own_code(service):
    """
    Никаких чужих доменов и 'unsafe-inline': встроенные скрипт, стиль и
    обработчики кнопок разрешены по sha256, ровно те, что в разметке.
    """
    for path in ("/", "/privacy"):
        _, headers, text = service.request("GET", path)
        policy = headers["Content-Security-Policy"]
        assert "http" not in policy and "*" not in policy
        assert "'unsafe-inline'" not in policy and "'unsafe-eval'" not in policy
        for block in server._SCRIPT.findall(text) + server._STYLE.findall(text):
            assert server._sha(block) in policy
        for handler in server._HANDLER.findall(text):
            assert server._sha(handler) in policy
    # Ни одного адреса чужого сайта для загрузки: шрифты свои
    page = service.request("GET", "/")[2]
    assert "fonts.googleapis" not in page and "fonts.gstatic" not in page


def test_policy_follows_the_bot_name(service, monkeypatch):
    """Имя бота подставляется в скрипт страницы - хэш скрипта должен смениться с ним."""
    before = service.request("GET", "/")[1]["Content-Security-Policy"]
    monkeypatch.setattr(server, "BOT_NAME", "mira_other_bot")
    _, headers, text = service.request("GET", "/")
    assert headers["Content-Security-Policy"] != before
    [script] = server._SCRIPT.findall(text)
    assert server._sha(script) in headers["Content-Security-Policy"]


def test_fonts_are_served_from_own_address(service):
    """Шрифты страницы - свои файлы, без входа, с кэшем; чужих путей не отдаём."""
    page = service.request("GET", "/")[2]
    names = re.findall(r"url\((/fonts/[^)]+\.woff2)\)", page)
    assert len(names) == 4
    for name in names:
        code, headers, _ = service.request("GET", name)
        assert code == 200 and headers["Content-Type"] == "font/woff2"
        assert headers["Cache-Control"].startswith("public")
    for path in ("/fonts/../server.py", "/fonts/OFL-literata.txt", "/fonts/x.woff2"):
        assert service.request("GET", path)[0] in (401, 404)
    # Лицензия OFL лежит рядом с каждым шрифтом
    for family in ("golos-text", "literata"):
        assert "Open Font License" in (server.FONTS_DIR / f"OFL-{family}.txt").read_text()


def test_mark_is_served_without_login(service):
    """Знак Миры стоит и на экране входа: отдаётся без сессии, картинкой, с кэшем."""
    page = service.request("GET", "/")[2]
    assert 'src="/mira-mark.png"' in page
    code, headers, _ = service.request("GET", "/mira-mark.png")
    assert code == 200 and headers["Content-Type"] == "image/png"
    assert headers["Cache-Control"].startswith("public")


def test_stop_saves_diaries_and_releases_the_bot(tmp_path, monkeypatch):
    """
    SIGTERM: фоновые потоки получают сигнал, бот подтверждает Telegram
    сделанное, дневник, не дошедший до своего save(), всё равно на диске.
    """
    store = DiaryStore(tmp_path / "mira.db")
    halted = []

    class Bot:
        def halt(self, stop):
            halted.append(stop.is_set())

    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "SERVICE_BOT", Bot())
    monkeypatch.setattr(server, "STOP", threading.Event())
    diary = store.get(user_key(1))
    # Шаг, который поменял дневник, но до своего save() не дошёл
    diary.add(dialog.Parser().parse("Пил кофе, с утра тревога", diary.today().day))
    server.stop_service()

    assert server.STOP.is_set() and halted == [True]
    again = DiaryStore(tmp_path / "mira.db")
    assert again.get(user_key(1)).timeline.days


# ── находки аудита ────────────────────────────────────────────────────────

def test_deleted_rows_are_wiped_from_the_file(tmp_path):
    """secure_delete: удалённый текст не остаётся в файле свободными страницами."""
    store = DiaryStore(tmp_path / "mira.db")
    accounts = Accounts(store.storage)
    accounts.enroll(1)
    accounts.agree(1)
    diary = store.get(user_key(1))
    diary.add(dialog.Parser().parse("Пил кофе, сегодня секретслово", diary.today().day))
    diary.save()
    assert store.storage._db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    accounts.erase(1, store)
    store.close()
    assert b"\xd1\x81\xd0\xb5\xd0\xba\xd1\x80\xd0\xb5\xd1\x82" not in (tmp_path / "mira.db").read_bytes()


def test_old_free_pages_are_scrubbed_once(tmp_path):
    path = tmp_path / "mira.db"
    # База от прежней версии: удаляли без secure_delete, текст остался в файле
    db = sqlite3.connect(path)
    db.execute("PRAGMA secure_delete=OFF")
    db.execute("CREATE TABLE junk(t TEXT)")
    db.execute("INSERT INTO junk VALUES(?)", ("старыйхвост" * 200,))
    db.commit()
    db.execute("DELETE FROM junk")
    db.commit()
    db.close()
    assert "старыйхвост".encode() in path.read_bytes()
    store = DiaryStore(path)
    assert store.storage.scrub_once() is True
    assert store.storage.scrub_once() is False        # второй раз не переписываем
    store.close()
    assert "старыйхвост".encode() not in path.read_bytes()


def test_erase_is_one_transaction_and_failure_is_500(service, monkeypatch):
    alice = service.person(1)
    alice.post("/say", {"text": "Пил кофе, с утра тревога"})

    def broken(db, key):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service.store.storage, "drop_everything", broken)
    code, _, body = alice.post("/account/delete", {"confirm": "УДАЛИТЬ"})
    assert code == 500 and "ничего не удалено" in body
    # Откатилось целиком: человек, сессия и записи на месте
    assert service.accounts.user(1) is not None
    assert "кофе" in alice.get("/export?format=text")[2]
    monkeypatch.undo()


def test_sweep_removes_orphan_diaries_and_unconsented_accounts(service, monkeypatch):
    monkeypatch.setattr(server, "STORE", service.store)
    monkeypatch.setattr(server, "ACCOUNTS", service.accounts)
    alice = service.person(1)
    alice.post("/say", {"text": "Пил кофе, с утра тревога"})
    service.person(2, consent=False)
    # Дневник без хозяина: запись легла в гонке с удалением
    ghost = service.store.get(user_key(3))
    ghost.add(dialog.Parser().parse("Пил кофе", ghost.today().day))
    ghost.save()
    assert service.accounts.orphans() == [user_key(3)]

    server.sweep_idle()
    assert service.accounts.orphans() == []
    assert service.accounts.user(2) is not None      # неделя ещё не прошла
    week = time.time() + 8 * 24 * 60 * 60
    server.sweep_idle(now=week)
    assert service.accounts.user(2) is None
    assert service.accounts.user(1) is not None      # согласился - не трогаем


def test_request_of_a_just_deleted_user_is_401(service):
    """Сессия пережила человека: запрос в пути не заводит ему новый дневник."""
    alice = service.person(1)
    # Учётной записи уже нет, а сессия ещё в базе - удаление «в пути».
    # /export открыт без согласия, так что до дневника его пускает только
    # проверка в _user_key.
    with service.accounts.storage.atomic() as db:
        db.execute("DELETE FROM users WHERE user_id=1")
    code, _, _ = alice.get("/export?format=text")
    assert code == 401
    assert user_key(1) not in service.accounts.orphans()


def test_unexpected_error_is_a_quiet_500(service, monkeypatch, capsys):
    alice = service.person(1)

    def boom(*args, **kwargs):
        raise ValueError("Пил кофе, с утра тревога")      # текст человека в ошибке

    monkeypatch.setattr(server, "_say", boom)
    code, _, body = alice.post("/say", {"text": "Пил кофе"})
    assert code == 500 and "внутренняя ошибка" in body
    assert "кофе" not in body
    out = capsys.readouterr().out
    assert "ValueError #" in out and "кофе" not in out


def test_server_error_log_has_no_text(capsys):
    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    try:
        try:
            raise RuntimeError("Пил кофе, с утра тревога")
        except RuntimeError:
            httpd.handle_error(None, ("127.0.0.1", 1))
        try:
            raise BrokenPipeError()
        except BrokenPipeError:
            httpd.handle_error(None, ("127.0.0.1", 1))
    finally:
        httpd.server_close()
    out = capsys.readouterr().out
    assert out.count("RuntimeError #") == 1 and "кофе" not in out


def test_deeply_nested_json_is_400(service):
    alice = service.person(1)
    code, _, _ = alice.post("/say", b"[" * 60000 + b"]" * 60000)
    assert code in (400, 413)
    code, _, _ = alice.post("/say", b'{"text":' + b"[" * 30000 + b"]" * 30000 + b"}")
    assert code == 400


def test_idle_connection_is_closed():
    assert server.Handler.timeout == 30


def test_summary_takes_only_known_periods(service, monkeypatch):
    asked = []
    monkeypatch.setattr(server, "_summary_step",
                        lambda key, days, since=0: asked.append(days) or {"ok": True})
    alice = service.person(1)
    for days in (21, 180, 17, 365, 10 ** 9, "90", None):
        alice.post("/summary", {"days": days})
    assert asked == [21, 180, server.DEFAULT_DAYS, server.DEFAULT_DAYS,
                     server.DEFAULT_DAYS, 90, server.DEFAULT_DAYS]


@pytest.mark.parametrize("path, body, limit", [
    ("/summary", {"days": 21}, 10),
    ("/food", {"калории": 2000}, 10),
    ("/city", {"city": "Москва"}, 10),
])
def test_heavy_requests_are_limited_per_person(service, devices_on, monkeypatch, path, body, limit):
    monkeypatch.setattr(server, "_summary_step", lambda *a, **k: {"ok": True})
    alice, bob = service.person(1), service.person(2)
    for _ in range(limit):
        assert alice.post(path, body)[0] == 200
    code, headers, _ = alice.post(path, body)
    assert code == 429 and headers.get("Retry-After")
    assert bob.post(path, body)[0] == 200             # потолок у каждого свой


def test_food_takes_only_last_two_years(service, devices_on):
    from datetime import date, timedelta
    alice = service.person(1)
    today = date.today()
    rows = ["дата,калории",
            f"{(today - timedelta(days=10)).isoformat()},2000",
            f"{(today + timedelta(days=5)).isoformat()},2000",
            "1900-01-01,2000",
            f"{(today - timedelta(days=800)).isoformat()},2000"]
    code, _, body = alice.post("/food", {"csv": "\n".join(rows)})
    assert code == 200 and json.loads(body)["days"] == 1
    days = {r.day for r in service.store.get(user_key(1)).timeline.days}
    assert days == {today - timedelta(days=10)}
    _, _, body = alice.post("/food", {"csv": "дата,калории\n1900-01-01,2000"})
    assert "двух лет" in json.loads(body)["error"]


def test_food_respects_the_diary_ceiling(service, devices_on, monkeypatch):
    from datetime import date, timedelta
    monkeypatch.setattr(server, "MAX_DIARY_DAYS", 3)
    alice = service.person(1)
    today = date.today()
    rows = ["дата,калории"] + [f"{(today - timedelta(days=i)).isoformat()},2000"
                               for i in range(5)]
    _, _, body = alice.post("/food", {"csv": "\n".join(rows)})
    assert json.loads(body)["ok"] is False
    assert not service.store.get(user_key(1)).timeline.days


def test_export_and_logout_work_without_consent(service):
    """Свои записи забрать можно и без нового согласия (сменился текст политики)."""
    alice = service.person(1)
    alice.post("/say", {"text": "Пил кофе, с утра тревога"})
    with service.accounts.storage.atomic() as db:
        db.execute("UPDATE users SET consent_version='старая' WHERE user_id=1")
    assert alice.post("/say", {"text": "Спал 7 часов"})[0] == 403
    code, _, body = alice.get("/export?format=text")
    assert code == 200 and "кофе" in body


def test_logout_everywhere_closes_every_session(service):
    alice = service.person(1)
    other = Person(service, 1)
    other.login(service.accounts.issue_login(1))
    bob = service.person(2)
    assert alice.post("/auth/logout-all")[0] == 200
    assert other.get("/state")[0] == 401 and alice.get("/state")[0] == 401
    assert bob.get("/state")[0] == 200


def test_page_says_who_is_logged_in(service):
    page = service.request("GET", "/")[2]
    assert 'id="whoami"' in page and "Вы вошли как @" in page
    assert "Выйти на всех устройствах" in page
