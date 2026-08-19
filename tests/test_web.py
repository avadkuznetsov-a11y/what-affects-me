"""Страница: кривые запросы, чужие сайты и токен, который никуда не утекает."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import web.server as server
from demo.generate import build
from wam import dialog
from wam.derive import derive_factors

FAKE_TOKEN = "7654321:" + "xxx-not-a-real-token" * 2


@pytest.fixture
def site():
    """Живой сервер на свободном порту. Разборщик - правила, в сеть не ходим."""
    server.PARSER = dialog.Parser()
    server.STORE.get(server.WEB_KEY).reset()
    server.RUNNER.disconnect()
    # Придуманный дневник за четыре месяца и статистику по нему подкладываем
    # готовыми: считать их тут незачем, проверяем не выводы, а обработку запросов.
    server._CACHE[server.DEFAULT_DAYS] = (derive_factors(build(days=21)), [])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _fetch(url, body=None, headers=None, method=None):
    """Ответ как (код, текст). Отказы тоже интересны, поэтому ловим HTTPError."""
    data = None if body is None else (body if isinstance(body, bytes)
                                      else json.dumps(body).encode())
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")


def test_broken_json_is_a_bad_request(site):
    code, _ = _fetch(site + "/say", "{ это не json".encode())
    assert code == 400


def test_too_big_body_is_refused(site):
    body = json.dumps({"text": "a" * (server.MAX_BODY + 100)}).encode()
    code, _ = _fetch(site + "/say", body)
    assert code == 413


def test_wrong_days_falls_back_to_default(site):
    assert server._days_param("/summary?days=abc") == server.DEFAULT_DAYS
    code, body = _fetch(site + "/say", {"text": "Пил кофе, с утра тревога", "days": "abc"})
    assert code == 200 and json.loads(body)["messages"]


def test_unknown_path_is_not_found(site):
    assert _fetch(site + "/nothing-here")[0] == 404
    assert _fetch(site + "/telegram/nothing-here", {})[0] == 404


def test_ring_with_text_instead_of_number_is_a_bad_request(site):
    """Раньше строка в показаниях кольца доходила до float() и роняла запрос."""
    code, _ = _fetch(site + "/say", {"text": "Пил кофе", "ring": {"sleep_score": "abc"}})
    assert code == 400
    assert _fetch(site + "/say", {"text": "Пил кофе", "ring": [1, 2]})[0] == 400


def test_page_from_another_site_is_refused(site):
    code, _ = _fetch(site + "/state", headers={"Origin": "http://evil.example"})
    assert code == 403
    code, _ = _fetch(site + "/say", {"text": "привет"},
                     headers={"Origin": "http://evil.example"})
    assert code == 403


def test_state_never_contains_the_token(site):
    """Токен живёт в памяти процесса и не появляется ни в одном ответе."""
    server.RUNNER._token = FAKE_TOKEN     # как будто бот уже подключён
    server.RUNNER.username = "mira_test_bot"
    server.RUNNER.code = "4821-МИРА"

    code, body = _fetch(site + "/state")
    assert code == 200
    assert FAKE_TOKEN not in body and "not-a-real" not in body
    state = json.loads(body)["telegram"]
    assert state["username"] == "mira_test_bot" and state["code"] == "4821-МИРА"


def test_bad_token_is_refused_without_network(site):
    nonsense = "просто слова"
    code, body = _fetch(site + "/telegram/connect", {"token": nonsense})
    assert code == 200
    answer = json.loads(body)
    assert answer["ok"] is False and "BotFather" in answer["error"]


def test_reset_needs_post(site):
    assert _fetch(site + "/reset")[0] == 404          # раньше сброс делался по GET
    assert _fetch(site + "/reset", {})[0] == 200


def test_chat_messages_show_up_in_state(site):
    """Лента страницы забирает то, что пришло из чата, по номеру сообщения."""
    diary = server.STORE.get(server.WEB_KEY)
    _fetch(site + "/say", {"text": "Пил кофе, с утра тревога"})
    mark = diary.seq
    dialog.step(diary, "Была тренировка, настроение хорошее",
                parser=dialog.Parser(), origin="chat")

    code, body = _fetch(site + f"/state?since={mark}")
    fresh = json.loads(body)["messages"]
    assert code == 200 and fresh
    assert all(m["from"] == "chat" for m in fresh)
