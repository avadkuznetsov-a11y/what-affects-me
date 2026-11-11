"""
Бот сервиса (режим server): /start даёт ссылку входа, без согласия ничего не
пишется, у каждого свой дневник, /delete стирает всё, журнал без текста.

Telegram подменён так же, как в test_bot.py: вместо сети - функция, которая
складывает вызовы и отдаёт заготовленные ответы.
"""
import json
import sqlite3
import threading
from datetime import date, datetime, time
from urllib.parse import urlparse

import pytest

from bot import telegram
from bot.telegram import ERASE_WORD, CHAT_LIMIT, TelegramBot
from wam import dialog, limits
from wam.accounts import Accounts, user_key
from wam.diary import DiaryStore

FAKE_TOKEN = "1234567:" + "a" * 35
PUBLIC = "https://mira.test"


class FakeTelegram:
    """Подмена API: запоминает каждый вызов, на getUpdates отдаёт очередь."""

    def __init__(self, updates=None):
        self.calls: list[tuple[str, dict]] = []
        self.updates = list(updates or [])

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return {"ok": True, "result": batch}
        if method == "getMe":
            return {"ok": True, "result": {"username": "mira_test_bot"}}
        return {"ok": True, "result": True}

    def sent(self, chat_id=None) -> list[dict]:
        return [params for method, params in self.calls
                if method == "sendMessage" and (chat_id is None or params["chat_id"] == chat_id)]


@pytest.fixture
def world(tmp_path):
    store = DiaryStore(tmp_path / "mira.db")
    accounts = Accounts(store.storage)
    bot = TelegramBot(token=FAKE_TOKEN, store=store, parser=dialog.Parser(),
                      accounts=accounts, public_url=PUBLIC)
    api = FakeTelegram()
    bot._call = api
    return bot, store, accounts, api


def _update(text, user_id=101, update_id=1, chat_type="private", chat_id=None, **extra):
    message = {"chat": {"id": user_id if chat_id is None else chat_id, "type": chat_type},
               "from": {"id": user_id, "username": f"user{user_id}"}, "text": text}
    message.update(extra)
    return {"update_id": update_id, "message": message}


def _say(bot, text, user_id=101):
    bot.process(_update(text, user_id))


def _token_from(text: str) -> str:
    link = next(word for word in text.split() if word.startswith(PUBLIC + "/login#t="))
    return urlparse(link).fragment.split("=", 1)[1]


def _consent(bot, user_id=101):
    bot.process({"update_id": 99, "callback_query": {
        "id": "q1", "data": "consent", "from": {"id": user_id}}})


def _ready(bot, user_id=101):
    """Человек написал /start и нажал «Согласен»."""
    _say(bot, "/start", user_id)
    _consent(bot, user_id)


def test_start_sends_a_one_time_link_without_preview(world):
    bot, store, accounts, api = world
    _say(bot, "/start")
    [message] = api.sent(101)
    token = _token_from(message["text"])
    assert json.loads(message["link_preview_options"]) == {"is_disabled": True}
    # человек заведён, чат привязан к его дневнику, ссылка ведёт к нему
    assert accounts.user(101)["username"] == "user101"
    assert store.linked_chats(user_key(101)) == [101]
    assert accounts.redeem_login(token) == 101
    assert accounts.redeem_login(token) is None          # второй раз - нет


def test_polling_loop_answers_start_with_a_link(tmp_path):
    """Тот же путь через цикл опроса: getUpdates → /start → sendMessage со ссылкой."""
    store = DiaryStore(tmp_path / "mira.db")
    accounts = Accounts(store.storage)
    bot = TelegramBot(token=FAKE_TOKEN, store=store, parser=dialog.Parser(),
                      accounts=accounts, public_url=PUBLIC)
    api = FakeTelegram([_update("/start", update_id=5)])
    stop = threading.Event()

    def call(method, **params):
        answer = api(method, **params)
        if method == "getUpdates" and len([c for c in api.calls if c[0] == "getUpdates"]) > 1:
            stop.set()
        return answer

    bot._call = call
    bot.run(stop=stop)
    [message] = api.sent(101)
    assert accounts.redeem_login(_token_from(message["text"])) == 101


def test_service_commands_are_published_in_latin(world):
    bot, _, _, api = world
    assert bot.username() == "mira_test_bot"
    commands = json.loads(dict(api.calls)["setMyCommands"]["commands"])
    names = {c["command"] for c in commands}
    assert {"login", "delete", "export"} <= names
    assert all(name.isascii() for name in names)


def test_login_gives_a_new_link_and_kills_the_old(world):
    bot, _, accounts, api = world
    _say(bot, "/start")
    old = _token_from(api.sent(101)[-1]["text"])
    _say(bot, "/login")
    new = _token_from(api.sent(101)[-1]["text"])
    assert accounts.redeem_login(old) is None
    assert accounts.redeem_login(new) == 101


def test_nothing_is_written_before_consent(world):
    bot, store, accounts, api = world
    _say(bot, "/start")
    _say(bot, "Пил кофе, с утра тревога")
    answer = api.sent(101)[-1]
    assert "согласие" in answer["text"]
    assert json.loads(answer["reply_markup"])["inline_keyboard"][0][0]["callback_data"] == "consent"
    assert not store.get(user_key(101)).timeline.days
    assert bot.reminders.chats() == []          # и напоминать пока некому


def test_consent_button_opens_the_diary(world):
    bot, store, accounts, api = world
    _ready(bot)
    assert accounts.has_consent(101)
    assert any(method == "answerCallbackQuery" for method, _ in api.calls)
    _say(bot, "Пил кофе, с утра тревога")
    assert store.get(user_key(101)).today().factor("кофе") == 1.0
    assert bot.reminders.chats() == [101]


def test_voice_is_not_even_downloaded_before_consent(world, monkeypatch):
    """Голос на сервисе выключен, но когда его включат - без согласия ни байта."""
    monkeypatch.setattr(telegram, "SERVICE_VOICE", True)
    bot, _, _, api = world
    _say(bot, "/start")
    downloads = []
    bot.download = lambda file_id: downloads.append(file_id) or b""
    bot.process(_update("", voice={"file_id": "voice-1"}))
    assert downloads == []
    assert "согласие" in api.sent(101)[-1]["text"]


def test_stranger_is_asked_to_start(world):
    bot, store, _, api = world
    _say(bot, "Пил кофе")
    assert "/start" in api.sent(101)[-1]["text"]
    assert not store.get(user_key(101)).timeline.days


def test_group_chats_are_ignored(world):
    bot, store, accounts, api = world
    bot.process(_update("/start", chat_type="group", chat_id=-500))
    bot.process(_update("Пил кофе", chat_type="group", chat_id=-500))
    assert api.sent() == []
    assert accounts.user(101) is None


def test_two_people_have_two_diaries_and_no_guests(world):
    bot, store, _, _ = world
    _ready(bot, 101)
    _ready(bot, 202)
    _say(bot, "Пил кофе, с утра тревога", 101)
    _say(bot, "Ходил в зал", 202)
    assert store.get(user_key(101)).today().factor("кофе") == 1.0
    assert store.get(user_key(202)).today().factor("кофе") is None
    # гостевых дневников tg:* в режиме server не бывает
    assert not [key for key in store._diaries if key.startswith("tg:")]
    assert not store._guests


def test_codes_from_the_page_are_not_accepted(world):
    """Код привязки - механизм режима local. В server он просто текст дня."""
    bot, store, _, api = world
    _ready(bot)
    code = store.new_code("web")
    _say(bot, code)
    assert store.key_for_chat(101) == user_key(101)


def _rows(store, user_id) -> dict:
    """Сколько строк о человеке осталось в каждой таблице базы."""
    key = user_key(user_id)
    with store.storage.atomic() as db:
        return {
            "users": db.execute("SELECT COUNT(*) FROM users WHERE user_id=?", (user_id,)).fetchone()[0],
            "sessions": db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)).fetchone()[0],
            "login_tokens": db.execute("SELECT COUNT(*) FROM login_tokens WHERE user_id=?", (user_id,)).fetchone()[0],
            "reminders": db.execute("SELECT COUNT(*) FROM reminders WHERE chat_id=?", (user_id,)).fetchone()[0],
            "chats": db.execute("SELECT COUNT(*) FROM chats WHERE chat_id=? OR diary=?", (user_id, key)).fetchone()[0],
            "diaries": db.execute("SELECT COUNT(*) FROM diaries WHERE diary=?", (key,)).fetchone()[0],
            "days": db.execute("SELECT COUNT(*) FROM days WHERE diary=?", (key,)).fetchone()[0],
            "facts": db.execute("SELECT COUNT(*) FROM facts WHERE diary=?", (key,)).fetchone()[0],
        }


def test_delete_with_confirmation_wipes_every_table(world):
    bot, store, accounts, api = world
    for who in (101, 202):
        _ready(bot, who)
        _say(bot, "Пил кофе, с утра тревога", who)
        _say(bot, "/reminder 20:30", who)
        accounts.open_session(who)
    accounts.issue_login(101)
    before = _rows(store, 101)
    assert all(before[table] for table in ("users", "sessions", "login_tokens", "reminders",
                                           "chats", "diaries", "days", "facts")), before

    _say(bot, "/delete")
    assert ERASE_WORD in api.sent(101)[-1]["text"]
    _say(bot, ERASE_WORD)
    assert "удалены" in api.sent(101)[-1]["text"]

    assert all(count == 0 for count in _rows(store, 101).values()), _rows(store, 101)
    assert not store.get(user_key(101)).timeline.days
    assert 101 not in bot.reminders.chats()
    # второго человека удаление не тронуло
    assert all(_rows(store, 202)[table] for table in ("users", "sessions", "reminders",
                                                      "chats", "diaries", "days", "facts"))
    assert store.get(user_key(202)).today().factor("кофе") == 1.0
    # и после перезапуска удалённое не вернулось
    store.storage.close()
    again = DiaryStore(store.storage.path)
    assert not again.get(user_key(101)).timeline.days
    assert again.get(user_key(202)).today().factor("кофе") == 1.0


def test_delete_needs_the_word(world):
    bot, store, accounts, _ = world
    _ready(bot)
    _say(bot, "Пил кофе, с утра тревога")
    _say(bot, "/delete")
    _say(bot, "нет, передумал")
    _say(bot, ERASE_WORD)               # просьба уже забыта
    assert accounts.user(101) is not None
    assert store.get(user_key(101)).today().factor("кофе") == 1.0


def test_delete_works_without_consent(world):
    bot, _, accounts, _ = world
    _say(bot, "/start")
    _say(bot, "/delete")
    _say(bot, ERASE_WORD)
    assert accounts.user(101) is None


def test_too_many_messages_are_dropped(world):
    bot, store, _, api = world
    _ready(bot)
    api.calls.clear()
    for _ in range(CHAT_LIMIT + 5):
        _say(bot, "/help")
    # два места в окне уже заняты: /start и нажатие «Согласен» в _ready
    assert len(api.sent(101)) == CHAT_LIMIT - 2


def test_log_has_no_user_text(world, capsys):
    bot, _, _, _ = world
    _ready(bot)

    def broken(*args, **kwargs):
        raise ValueError("не разобрал: «поругался с женой, болит сердце»")

    bot.parser.parse = broken
    _say(bot, "поругался с женой, болит сердце")
    out = capsys.readouterr().out
    assert "Ошибка на сообщении: ValueError #" in out
    assert "сердце" not in out and "женой" not in out


def test_reminders_survive_a_restart(world):
    bot, store, accounts, _ = world
    _ready(bot, 101)
    _ready(bot, 202)
    _say(bot, "/reminder 20:30", 101)
    _say(bot, "/reminder выкл", 202)
    bot.remind(datetime.combine(date.today(), time(21, 0)))

    again = TelegramBot(token=FAKE_TOKEN, store=store, parser=dialog.Parser(),
                        accounts=accounts, public_url=PUBLIC)
    assert sorted(again.reminders.chats()) == [101, 202]
    assert again.reminders.time_of(101) == "20:30"
    assert not again.reminders.is_on(202)
    # сегодня 101-му уже напомнили - второго раза после перезапуска не будет
    assert not again.reminders.due(101, datetime.combine(date.today(), time(22, 0)))


def test_reminders_have_no_cap_in_server_mode(world):
    bot, _, _, _ = world
    for chat in range(1, 260):
        bot.reminders.remember(chat)
    assert len(bot.reminders.chats()) == 259


def test_bot_shares_the_daily_parse_limit(world, monkeypatch):
    """Потолок разбора - на человека, в сумме со страницей: тот же счётчик."""
    from wam import limits

    monkeypatch.setattr(limits, "SAY_PER_DAY", limits.Limiter(2, 24 * 3600))
    bot, store, _, api = world
    _ready(bot)
    assert limits.SAY_PER_DAY.allow(101)            # одна реплика ушла со страницы
    _say(bot, "Пил кофе, с утра тревога")
    _say(bot, "Ходил в зал")
    assert "завтра" in api.sent(101)[-1]["text"]
    assert store.get(user_key(101)).today().factor("тренировка") is None


# ── остановка процесса ────────────────────────────────────────────────────

def test_halt_confirms_what_was_done(world):
    """
    Обработанные апдейты Telegram считает полученными только со следующим
    getUpdates. При остановке его делает halt(), иначе после перезапуска бот
    ответил бы на ту же пачку второй раз.
    """
    bot, _, _, api = world
    api.updates = [_update("/start", update_id=7)]
    stop = threading.Event()

    def call(method, **params):
        answer = api(method, **params)
        if method == "getUpdates" and len([c for c in api.calls if c[0] == "getUpdates"]) > 1:
            stop.set()
        return answer

    bot._call = call
    bot.run(stop=stop)
    api.calls.clear()
    bot._call = api
    bot.halt(stop)
    assert api.calls == [("getUpdates", {"offset": 8, "timeout": 0})]


def test_stopped_mid_batch_leaves_the_rest_to_telegram(world):
    """Остановили посреди пачки - остаток не обрабатываем и не подтверждаем."""
    bot, _, _, api = world
    stop = threading.Event()
    api.updates = [_update("/start", update_id=1), _update("/start", user_id=102, update_id=2)]
    handled = []

    def process(update):
        handled.append(update["update_id"])
        stop.set()                       # SIGTERM пришёл, пока шёл первый

    bot.process = process
    bot.run(stop=stop)
    assert handled == [1]
    bot.halt(stop)
    assert api.calls[-1] == ("getUpdates", {"offset": 2, "timeout": 0})


def test_halt_waits_for_the_message_in_work(world):
    bot, _, _, api = world
    started, release = threading.Event(), threading.Event()
    stop = threading.Event()
    api.updates = [_update("/start", update_id=3)]

    def process(update):
        started.set()
        release.wait(5)

    bot.process = process
    worker = threading.Thread(target=bot.run, kwargs={"stop": stop})
    worker.start()
    assert started.wait(5)
    halter = threading.Thread(target=bot.halt, args=(stop,))
    halter.start()
    halter.join(0.3)
    assert halter.is_alive()             # ждёт, пока сообщение не доделано
    release.set()
    halter.join(5)
    worker.join(5)
    assert not halter.is_alive() and not worker.is_alive()
    assert ("getUpdates", {"offset": 4, "timeout": 0}) in api.calls


# ── голос на сервисе ──────────────────────────────────────────────────────

def _agreed(bot, user_id=101):
    _say(bot, "/start", user_id=user_id)
    bot.accounts.agree(user_id)


def test_service_voice_is_politely_refused_without_download(world, monkeypatch):
    """SaluteSpeech на сервере не включаем: голосовое не скачиваем, просим текст."""
    monkeypatch.setenv("SALUTE_SPEECH_TOKEN", "key-for-test")
    bot, store, accounts, api = world
    again = TelegramBot(token=FAKE_TOKEN, store=store, parser=dialog.Parser(),
                        accounts=accounts, public_url=PUBLIC)
    assert again.speech is None          # ключ есть, а распознавания нет
    _agreed(bot)
    downloads = []
    bot.download = lambda file_id: downloads.append(file_id) or b""
    bot.process(_update("", voice={"file_id": "voice-1", "duration": 5}))
    assert downloads == []
    assert api.sent(101)[-1]["text"] == telegram.VOICE_TEXT_ONLY


class _Speech:
    def __init__(self):
        self.heard = []

    def transcribe(self, audio, fmt):
        self.heard.append(audio)
        return "Пил кофе, с утра тревога"


@pytest.mark.parametrize("voice", [{"file_id": "v", "duration": 600},
                                   {"file_id": "v", "duration": 5,
                                    "file_size": telegram.MAX_FILE + 1}])
def test_long_voice_is_refused_before_download(world, monkeypatch, voice):
    monkeypatch.setattr(telegram, "SERVICE_VOICE", True)
    bot, _, _, api = world
    bot.speech = _Speech()
    _agreed(bot)
    downloads = []
    bot.download = lambda file_id: downloads.append(file_id) or b"ogg"
    bot.process(_update("", voice=voice))
    assert downloads == []
    assert "двух минут" in api.sent(101)[-1]["text"]


def test_voice_parse_limit_is_checked_before_download_and_counted_once(world, monkeypatch):
    monkeypatch.setattr(telegram, "SERVICE_VOICE", True)
    bot, _, _, api = world
    bot.speech = _Speech()
    _agreed(bot)
    counted = []
    monkeypatch.setattr(limits, "may_parse", lambda uid: counted.append(uid) or "")
    downloads = []
    bot.download = lambda file_id: downloads.append(file_id) or b"ogg"
    bot.process(_update("", voice={"file_id": "v", "duration": 5}))
    assert downloads == ["v"] and counted == [101]

    monkeypatch.setattr(limits, "may_parse", lambda uid: "Слишком много сообщений подряд.")
    bot.process(_update("", voice={"file_id": "v2", "duration": 5}))
    assert downloads == ["v"]
    assert "Слишком много" in api.sent(101)[-1]["text"]


def test_failed_erase_is_not_reported_as_done(world, monkeypatch):
    bot, store, accounts, api = world
    _agreed(bot)
    _say(bot, "Пил кофе, с утра тревога")

    def broken(db, key):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store.storage, "drop_everything", broken)
    _say(bot, "/delete")
    _say(bot, ERASE_WORD)
    assert "ничего не удалено" in api.sent(101)[-1]["text"]
    assert accounts.user(101) is not None          # откатилось целиком
    assert store.get(user_key(101)).timeline.days
