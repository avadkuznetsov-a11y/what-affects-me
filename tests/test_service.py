"""
Режим server: вход по ссылке, сессии, CSRF и главное - чужой дневник не видно.

Сервер поднимается настоящий, на свободном порту, но с подменённым режимом и
своей базой во временном каталоге. Запросы собираются руками через
http.client: так видно каждый заголовок - Host, Origin, Cookie, X-CSRF, - и
можно отправить ровно тот кривой запрос, от которого защищаемся.
"""
import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import web.server as server
import wam.accounts as accounts_module
from demo.generate import build
from wam import dialog, weather
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
    monkeypatch.setattr(server, "PUBLIC_HOST", HOST)
    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "ACCOUNTS", accounts)
    monkeypatch.setattr(server, "PARSER", dialog.Parser())
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


def test_food_is_separate(service):
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
