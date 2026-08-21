"""Страница: кривые запросы, чужие сайты и токен, который никуда не утекает."""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

import pytest

import web.server as server
from demo.generate import build
from wam import dialog, weather
from wam.derive import derive_factors
from wam.diary import CODE_TRIES_TOTAL, Diary, DiaryStore
from wam.storage import DIARY_FILE

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


def test_page_comes_out_whole(site):
    """
    Разметку страницы правят руками, а подстановки в ней делает обычный replace.
    Стоит переименовать литерал или id - и страница молча перестанет работать:
    ни одна другая проверка этого не увидит.
    """
    code, html = _fetch(site + "/")
    assert code == 200
    for literal in ("__ENGINE__", "__PERIODS__", "__DEFAULT_DAYS__"):
        assert literal not in html
    assert 'id="feed"' in html


def test_broken_json_is_a_bad_request(site):
    code, _ = _fetch(site + "/say", "{ это не json".encode())
    assert code == 400


def test_too_big_body_is_refused(site):
    body = json.dumps({"text": "a" * (server.MAX_BODY + 100)}).encode()
    code, _ = _fetch(site + "/say", body)
    assert code == 413


def test_wrong_days_falls_back_to_default(site):
    assert server._clamp_days("abc") == server.DEFAULT_DAYS
    code, body = _fetch(site + "/say", {"text": "Пил кофе, с утра тревога", "days": "abc"})
    assert code == 200 and json.loads(body)["messages"]


def test_summary_needs_post(site):
    """Выводы дописываются в ленту, значит чужая страница не должна их звать."""
    assert _fetch(site + "/summary?days=21")[0] == 404
    code, body = _fetch(site + "/summary", {"days": server.DEFAULT_DAYS})
    assert code == 200 and json.loads(body)["messages"]


def test_period_is_named_in_human_words(site):
    """«21 дней» и «6 месяца» - так не говорят."""
    assert server.period_name(21) == "3 недели"        # слова из списка на странице
    assert server.period_name(180) == "полгода"
    assert server.period_name(22) == "22 дня"          # срок не из списка - по-русски
    assert server.period_name(91) == "3 месяца"


def test_unknown_path_is_not_found(site):
    assert _fetch(site + "/nothing-here")[0] == 404
    assert _fetch(site + "/telegram/nothing-here", {})[0] == 404


def test_ring_with_text_instead_of_number_is_a_bad_request(site):
    """Раньше строка в показаниях кольца доходила до float() и роняла запрос."""
    code, _ = _fetch(site + "/say", {"text": "Пил кофе", "ring": {"sleep_score": "abc"}})
    assert code == 400
    assert _fetch(site + "/say", {"text": "Пил кофе", "ring": [1, 2]})[0] == 400


def test_impossible_ring_reading_is_dropped(site):
    """
    Показание не по шкале выкидываем поодиночке, а рассказ записываем: прежде
    одно кривое поле отвечало четырёхсоткой и человек терял весь текст дня.
    Заодно потолок у каждого показателя свой: пульс 120 - это норма, а не бред.
    """
    diary = server.STORE.get(server.WEB_KEY)
    code, _ = _fetch(site + "/say", {"text": "Пил кофе, с утра тревога",
                                     "ring": {"sleep_score": 999999, "steps": 200000}})
    assert code == 200
    assert diary.today().factor("кофе") == 1.0              # рассказ на месте
    assert diary.today().metric("качество сна") is None     # а бессмыслицы нет

    code, _ = _fetch(site + "/say", {"text": "Тренировка", "ring": {"resting_hr": 120}})
    assert code == 200 and diary.today().metric("пульс покоя") is not None

    # Правдоподобные показания проходят: шкалы - до сотни, шаги - до сотни тысяч
    code, _ = _fetch(site + "/say", {"text": "Пил кофе",
                                     "ring": {"sleep_score": 80, "steps": 12000}})
    assert code == 200
    assert diary.today().metric("качество сна") == 8.0


def test_negative_ring_reading_is_dropped(site):
    """Проверка по модулю пропускала -100 и давала «качество сна -100 из 10»."""
    assert server._checked({"text": "", "ring": {"sleep_score": -100,
                                                 "steps": -50000}})[1] == {}
    code, _ = _fetch(site + "/say", {"text": "Пил кофе", "ring": {"sleep_score": -100}})
    assert code == 200
    assert server.STORE.get(server.WEB_KEY).today().metric("качество сна") is None


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
    server.RUNNER.code = server.STORE.new_code(server.WEB_KEY)

    code, body = _fetch(site + "/state")
    assert code == 200
    assert FAKE_TOKEN not in body and "not-a-real" not in body
    state = json.loads(body)["telegram"]
    assert state["username"] == "mira_test_bot"
    assert state["code"] == server.RUNNER.code


def test_stale_code_is_not_shown(site):
    """Просроченный код в панели - ловушка: бот на него уже не отзовётся."""
    server.RUNNER.code = "7QK4-M2XB"      # такого кода хранилище не выдавало

    state = json.loads(_fetch(site + "/state")[1])["telegram"]
    assert state["code"] == "" and state["code_stale"] is True


def test_code_burned_by_guessing_is_not_shown(site):
    """
    Перебор из многих чатов гасит все коды в хранилище. Панель про это узнаёт
    только если спрашивает хранилище: по своим часам код ещё «свежий».
    """
    store = server.STORE
    server.RUNNER.code = store.new_code(server.WEB_KEY)
    assert json.loads(_fetch(site + "/state")[1])["telegram"]["code"] == server.RUNNER.code

    for chat in range(CODE_TRIES_TOTAL):
        store.bind("AAAA-BBBB", 2000 + chat)

    state = json.loads(_fetch(site + "/state")[1])["telegram"]
    assert state["code"] == "" and state["code_stale"] is True


def test_linked_chat_can_be_unlinked(site):
    """Отозвать доступ у чата можно, не выключая бота."""
    store = server.STORE
    store.bind(store.new_code(server.WEB_KEY), 555)
    state = json.loads(_fetch(site + "/state")[1])["telegram"]
    assert state["chats"] == [555] and state["linked"] is True

    assert _fetch(site + "/telegram/unlink", {"chat": 555})[0] == 200
    assert store.linked_chats(server.WEB_KEY) == []
    state = json.loads(_fetch(site + "/state")[1])["telegram"]
    assert state["chats"] == [] and state["linked"] is False


def test_unlink_with_nonsense_chat_is_a_bad_request(site):
    assert _fetch(site + "/telegram/unlink", {"chat": "все"})[0] == 400


def test_unlinking_does_not_hand_out_a_new_code(site):
    """
    «Отвязать» закрывает доступ, а не открывает новое окно привязки: код после
    отвязки появляется только по кнопке «Показать новый код».
    """
    store = server.STORE
    stop = threading.Event()
    server.RUNNER._thread = threading.Thread(target=stop.wait, daemon=True)
    server.RUNNER._thread.start()      # как будто опрос Telegram идёт
    try:
        server.RUNNER.code = store.new_code(server.WEB_KEY)
        store.bind(server.RUNNER.code, 555)

        assert _fetch(site + "/telegram/unlink", {"chat": 555})[0] == 200
        state = json.loads(_fetch(site + "/state")[1])["telegram"]
        assert state["chats"] == [] and state["code"] == ""
        assert server.RUNNER.new_code()          # а по кнопке код по-прежнему дают
    finally:
        stop.set()
        server.RUNNER._thread = None


def test_someone_elses_chat_is_not_unlinked(site):
    """Номер чужого чата в запросе не должен рвать чужую привязку."""
    store = server.STORE
    store.bind(store.new_code("tg:999"), 555)
    try:
        assert _fetch(site + "/telegram/unlink", {"chat": 555})[0] == 200
        assert store.key_for_chat(555) == "tg:999"
    finally:
        store.unlink("tg:999")      # хранилище одно на все проверки


def test_disconnect_closes_access_to_the_diary(site):
    """«Отключить» отзывает и привязку: иначе доступ у чата остаётся навсегда."""
    store = server.STORE
    store.bind(store.new_code(server.WEB_KEY), 777)
    assert _fetch(site + "/telegram/disconnect", {})[0] == 200
    assert store.linked_chats(server.WEB_KEY) == []


def test_request_from_another_site_without_origin_is_refused(site):
    """Картинка с чужого сайта Origin не шлёт, а Sec-Fetch-Site шлёт."""
    code, _ = _fetch(site + "/state", headers={"Sec-Fetch-Site": "cross-site"})
    assert code == 403


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


def test_message_from_the_chat_after_the_answer_is_not_lost(monkeypatch, site):
    """
    Пока шаг разговора заканчивается, в ленту успевает лечь сообщение из чата.
    Оно уезжает к странице вместе с ответом: лента и её номер читаются под одним
    замком, поэтому за номером в ответе непоказанного не остаётся.
    """
    diary = server.STORE.get(server.WEB_KEY)
    real_step = dialog.step

    def slipped(*args, **kwargs):
        messages = real_step(*args, **kwargs)
        with diary.lock:
            diary.say("me", "из чата", origin="chat")   # то самое сообщение в зазор
        return messages

    monkeypatch.setattr(server.dialog, "step", slipped)
    answer = server._say("Пил кофе", None, server.DEFAULT_DAYS)

    assert answer["seq"] == answer["messages"][-1]["seq"]
    assert answer["messages"][-1]["text"] == "из чата"
    assert diary.feed(answer["seq"]) == []


def test_message_from_the_chat_before_the_answer_is_not_lost(site):
    """
    Сообщение из чата пришло, а человек нажал «Отправить» раньше следующего
    опроса состояния. Страница шлёт свой lastSeq, сервер отдаёт ленту от него -
    иначе это сообщение не попало бы ни в ответ, ни потом в /state.
    """
    diary = server.STORE.get(server.WEB_KEY)
    code, body = _fetch(site + "/say", {"text": "Пил кофе, с утра тревога", "since": 0})
    shown = json.loads(body)["seq"]                 # это страница уже показала
    assert code == 200

    with diary.lock:
        diary.say("bot", "из чата", origin="chat")  # пришло между опросами

    code, body = _fetch(site + "/say", {"text": "на 7", "since": shown})
    answer = json.loads(body)
    assert code == 200
    assert "из чата" in [m["text"] for m in answer["messages"]]
    assert answer["seq"] == answer["messages"][-1]["seq"]


def test_summary_also_returns_the_feed_from_the_page_number(site):
    """Тот же зазор у кнопки «Что уже известно?»: лента идёт от lastSeq страницы."""
    diary = server.STORE.get(server.WEB_KEY)
    shown = json.loads(_fetch(site + "/say", {"text": "Пил кофе", "since": 0})[1])["seq"]
    with diary.lock:
        diary.say("bot", "из чата", origin="chat")

    body = _fetch(site + "/summary", {"days": server.DEFAULT_DAYS, "since": shown})[1]
    answer = json.loads(body)
    assert "из чата" in [m["text"] for m in answer["messages"]]
    assert answer["seq"] == answer["messages"][-1]["seq"]


def test_wrong_since_is_taken_as_the_beginning_of_the_feed(site):
    """Кривой номер - не повод отказывать: отдаём ленту с начала, повторы отобьёт страница."""
    assert server._seq("abc") == 0 and server._seq(-5) == 0 and server._seq(None) == 0
    assert server._seq(float("inf")) == 0 and server._seq(12) == 12
    code, body = _fetch(site + "/say", {"text": "Пил кофе", "since": "abc"})
    assert code == 200 and json.loads(body)["messages"]


def test_since_from_a_previous_run_does_not_leave_the_page_mute(site):
    """
    Вкладку не закрывали, а сервер перезапустили: страница шлёт свой прежний
    номер, а он больше нашего. По нему в ленте пусто - человек не видит даже
    собственного ответа. Такой номер считаем нулём и отдаём ленту с начала.
    """
    assert server._shown(10_000, 5) == 0 and server._shown(3, 5) == 3

    answer = json.loads(_fetch(site + "/say", {"text": "Пил кофе, с утра тревога",
                                               "since": 10_000})[1])
    # Лента с начала: своя же реплика человека в ней есть
    assert "Пил кофе, с утра тревога" in [m["text"] for m in answer["messages"]]

    state = json.loads(_fetch(site + "/state?since=10000")[1])
    assert state["messages"] and state["seq"] == answer["seq"]

    summary = json.loads(_fetch(site + "/summary", {"days": server.DEFAULT_DAYS,
                                                    "since": 10_000})[1])
    assert "Что уже известно?" in [m["text"] for m in summary["messages"]]


def test_state_reads_the_feed_and_the_number_together(monkeypatch, site):
    """
    Сообщение из чата, легшее между чтением ленты и чтением её номера, страница
    больше никогда не покажет. Растягиваем этот зазор и проверяем, что писателю
    в него не влезть.
    """
    diary = server.STORE.get(server.WEB_KEY)
    real_feed = Diary.feed

    def slow_feed(self, since=0):
        messages = real_feed(self, since)
        time.sleep(0.2)                 # растянутый зазор между лентой и номером
        return messages

    monkeypatch.setattr(Diary, "feed", slow_feed)
    with diary.lock:
        diary.say("me", "первое")

    answer = {}
    poller = threading.Thread(target=lambda: answer.update(
        json.loads(_fetch(site + "/state")[1])))
    poller.start()
    time.sleep(0.05)                    # обработчик успел дойти до чтения ленты
    with diary.lock:
        diary.say("bot", "из чата", origin="chat")
    poller.join(10)

    assert answer["seq"] == answer["messages"][-1]["seq"]


def test_refusal_reads_the_body_it_refuses(monkeypatch, site):
    """
    Отказ уходил, не вычитав тело. Пока соединение живёт дольше одного запроса,
    остаток тела разбирается как следующий запрос и клиент получает в ответ
    мусорный 400. Сервер отвечает по HTTP/1.0 и рвёт связь сам, поэтому здесь
    нарочно просим его держать соединение - иначе остаток не увидеть.
    """
    monkeypatch.setattr(server.Handler, "protocol_version", "HTTP/1.1")
    port = int(site.rsplit(":", 1)[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("POST", "/say", body=json.dumps({"text": "Пил кофе"}).encode(),
                           headers={"Origin": "http://evil.example"})
        refused = connection.getresponse()
        refused.read()
        assert refused.status == 403

        connection.request("POST", "/reset", body=b"{}")
        answer = connection.getresponse()
        answer.read()
        assert answer.status == 200
    finally:
        connection.close()


# ── погода для панели ─────────────────────────────────────────────────────

@pytest.fixture
def sky():
    """
    Чистая погода до и после проверки. Дневник в проверках один на всех, и
    забытый в нём город увёл бы соседний тест в настоящую сеть.
    """
    weather.forget()
    yield server.STORE.get(server.WEB_KEY)
    server.STORE.get(server.WEB_KEY).city = ""
    weather.forget()


def _sky_answer():
    """Ответ open-meteo за вчера и сегодня: давление за сутки прыгнуло на 7."""
    today = date.today()
    return {"daily": {
        "time": [(today - timedelta(days=1)).isoformat(), today.isoformat()],
        "temperature_2m_max": [21.0, 22.0],
        "temperature_2m_min": [12.0, 13.0],
        "pressure_msl_mean": [1004.0, 1011.0],
        "relative_humidity_2m_mean": [60, 62],
        "cloud_cover_mean": [40, 72],
    }}


def test_weather_shows_today_and_what_came_out_of_it(monkeypatch, site, sky):
    monkeypatch.setattr(weather, "_get", lambda url, params, timeout:
                        {"results": [{"latitude": 55.75, "longitude": 37.61}]}
                        if "geocoding" in url else _sky_answer())
    sky.city = "Москва"

    code, body = _fetch(site + "/weather", {})
    answer = json.loads(body)
    assert code == 200 and answer["ok"] is True
    assert answer["city"] == "Москва" and answer["day"] == date.today().isoformat()
    assert answer["today"]["давление"] == 1011.0
    assert answer["today"]["облачность"] == 72.0
    assert answer["change"]["давление"] == 7.0          # +7 к вчерашнему
    assert answer["factors"] == ["резкий перепад давления"]
    assert "низкое давление" in answer["checked"]       # проверено, но не сработало
    assert "скакнуло на 7 гПа" in answer["note"]
    assert answer["source"] == "open-meteo"


def test_weather_without_a_city_is_empty_and_not_an_error(monkeypatch, site, sky):
    def boom(*args, **kwargs):
        raise AssertionError("города нет, а в сеть пошли")

    monkeypatch.setattr(weather, "_get", boom)
    sky.city = ""

    code, body = _fetch(site + "/weather", {})
    answer = json.loads(body)
    assert code == 200 and answer["ok"] is True
    assert answer["city"] == "" and answer["day"] == ""
    assert answer["today"] == {} and answer["change"] == {}
    assert answer["factors"] == [] and answer["checked"] == [] and answer["note"] == ""


def test_weather_without_network_is_empty_and_not_an_error(monkeypatch, site, sky):
    monkeypatch.setattr(weather, "_get", lambda *args, **kwargs: None)
    sky.city = "Москва"

    code, body = _fetch(site + "/weather", {})
    answer = json.loads(body)
    assert code == 200 and answer["ok"] is True
    assert answer["city"] == "Москва" and answer["day"] == ""
    assert answer["today"] == {} and answer["factors"] == []


def test_weather_needs_post(site, sky):
    """Запрос ходит в сеть, значит чужая страница не должна звать его картинкой."""
    assert _fetch(site + "/weather")[0] == 404


def test_a_named_city_survives_a_restart(monkeypatch, site, sky):
    """Погоду человек настраивает один раз, а не после каждого перезапуска."""
    monkeypatch.setattr(weather, "_get", lambda *args, **kwargs: None)
    assert json.loads(_fetch(site + "/city", {"city": "Москва"})[1])["ok"] is True

    # Перезапуск - это новое хранилище на тот же файл
    again = DiaryStore(DIARY_FILE)
    assert again.get(server.WEB_KEY).city == "Москва"


def test_the_city_moves_out_of_the_old_file(monkeypatch, tmp_path, sky):
    """
    Город раньше лежал отдельным файлом. У того, кто запускал прототип до
    переезда, он остался на диске - и потерять его нельзя.
    """
    old = tmp_path / ".city"
    old.write_text("Казань", encoding="utf-8")
    monkeypatch.setattr(server, "CITY_FILE", old)

    server._move_city_from_file()

    assert server.STORE.get(server.WEB_KEY).city == "Казань"
    assert not old.exists()          # перенесли - файл больше не нужен
    again = DiaryStore(DIARY_FILE)
    assert again.get(server.WEB_KEY).city == "Казань"


def test_the_old_city_file_is_kept_when_saving_failed(monkeypatch, tmp_path, sky):
    """Пока город не записан, старый файл - единственное место, где он есть."""
    old = tmp_path / ".city"
    old.write_text("Казань", encoding="utf-8")
    monkeypatch.setattr(server, "CITY_FILE", old)
    monkeypatch.setattr(server.STORE.get(server.WEB_KEY), "save", lambda: False)

    server._move_city_from_file()

    assert old.exists() and old.read_text(encoding="utf-8") == "Казань"


def test_state_says_which_weather_service_works(monkeypatch, site):
    """По цифрам не понять, чья это погода, - панель должна знать источник."""
    monkeypatch.delenv(weather.YANDEX_KEY_ENV, raising=False)
    assert json.loads(_fetch(site + "/state")[1])["weather_source"] == "open-meteo"

    monkeypatch.setenv(weather.YANDEX_KEY_ENV, "ключа-в-репозитории-нет")
    assert json.loads(_fetch(site + "/state")[1])["weather_source"] == "Яндекс.Погода"


# ── еда числами ───────────────────────────────────────────────────────────

def test_food_numbers_become_factors_of_the_day(site):
    code, body = _fetch(site + "/food", {"калории": 3000, "белки": 40, "углеводы": 450})
    answer = json.loads(body)
    assert code == 200 and answer["ok"] and answer["days"] == 1

    record = server.STORE.get(server.WEB_KEY).today()
    assert record.factor("переел") == 1.0
    assert record.factor("мало белка") == 1.0
    assert record.factor("недоел") == 0.0        # дни без перекоса тоже записаны
    # Числа и то, что из них вышло, человек видит в ленте
    said = " ".join(m["text"] + m.get("note", "") for m in answer["messages"])
    assert "3000 ккал" in said and "переел" in said


def test_food_export_is_read_by_days(site):
    table = ("дата,калории,белки,жиры,углеводы\n"
             "2026-08-01,2900,70,95,410\n"
             "2026-08-02,1400,80,50,120\n")
    code, body = _fetch(site + "/food", {"csv": table})
    answer = json.loads(body)
    assert code == 200 and answer["ok"] and answer["days"] == 2
    assert answer["told"] == "2 дня"      # склонение считает программа, а не страница

    timeline = server.STORE.get(server.WEB_KEY).timeline
    days = {record.day.isoformat(): record for record in timeline.days}
    assert days["2026-08-01"].factor("переел") == 1.0
    assert days["2026-08-02"].factor("недоел") == 1.0


def test_nonsense_food_numbers_are_refused_with_a_reason(site):
    """Опечатка на лишний ноль не должна стать фактором дня."""
    code, body = _fetch(site + "/food", {"калории": "много"})
    assert code == 200 and json.loads(body)["ok"] is False
    code, body = _fetch(site + "/food", {"калории": 999999})
    assert json.loads(body)["ok"] is False
    code, body = _fetch(site + "/food", {})
    assert json.loads(body)["ok"] is False
