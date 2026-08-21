"""Бот: привязка чата, разбор сообщений, напоминания и стойкость цикла опроса."""
import threading
from datetime import date, datetime, time

from bot.telegram import TelegramBot, TelegramError, token_is_shaped_right
from wam import dialog
from wam.diary import CODE_TRIES_PER_CHAT, DiaryStore

# Токен ненастоящий, но правильной формы: настоящих в репозитории быть не должно.
FAKE_TOKEN = "1234567:" + "a" * 35



def _evening(hour: int) -> datetime:
    """
    Сегодняшний вечер. Дата в тестах не вбивается руками: «сегодня уже
    писал» сверяется с настоящим сегодня, и вчерашняя дата ломала тест
    на следующий же день.
    """
    return datetime.combine(date.today(), time(hour, 0))


def _bot(store=None, **kwargs):
    return TelegramBot(token=FAKE_TOKEN, store=store or DiaryStore(),
                       parser=dialog.Parser(), **kwargs)


def _message(text, chat_id=555, update_id=1):
    return {"update_id": update_id,
            "message": {"chat": {"id": chat_id}, "text": text}}


def test_token_shape_is_checked_before_network():
    assert token_is_shaped_right(FAKE_TOKEN)
    assert not token_is_shaped_right("")
    assert not token_is_shaped_right("просто слова")
    assert not token_is_shaped_right("1234567:коротко")


def test_username_comes_from_getme_without_showing_token():
    bot = _bot()
    seen = []

    def fake_call(method, **params):
        seen.append(method)
        return {"ok": True, "result": {"username": "mira_test_bot"}}

    bot._call = fake_call
    assert bot.username() == "mira_test_bot"
    assert seen == ["getMe"]


def test_error_text_never_shows_the_token():
    bot = _bot()
    assert FAKE_TOKEN not in bot._hide(f"getMe: не связи с https://x/bot{FAKE_TOKEN}/getMe")
    assert "***" in bot._hide(f"bot{FAKE_TOKEN}")


def test_code_binds_the_chat_and_next_phrase_goes_to_the_page_diary():
    store = DiaryStore()
    bot = _bot(store)
    code = store.new_code("web")

    answer = bot.handle(_message(code)["message"])
    assert "один дневник" in answer
    assert store.key_for_chat(555) == "web"

    bot.handle(_message("Пил кофе, с утра тревога")["message"])
    page = store.get("web")
    assert page.today().factor("кофе") == 1.0
    # и на странице видно, что это пришло из чата
    assert any(m["from"] == "chat" for m in page.messages)


def test_wrong_code_is_refused_politely():
    bot = _bot()
    answer = bot.handle(_message("AAAA-BBBB")["message"])
    assert "не подошёл" in answer


def test_bot_goes_quiet_when_the_code_is_being_guessed():
    """Ответ на каждый промах - подсказка подбирающему, что попытка засчитана."""
    bot = _bot()
    answers = [bot.handle(_message("AAAA-BBBB")["message"])
               for _ in range(CODE_TRIES_PER_CHAT)]
    assert answers[-1] == ""
    assert bot.handle(_message("AAAA-BBBB")["message"]) == ""


def test_update_without_chat_is_skipped():
    sent = []
    bot = _bot()
    bot.send = lambda chat_id, text: sent.append((chat_id, text))
    bot.process({"update_id": 1, "channel_post": {"text": "пост в канале"}})
    bot.process({"update_id": 2, "message": {"text": "без чата"}})
    assert sent == []


def test_one_broken_update_does_not_eat_the_rest():
    """Падение на одном сообщении не должно ни ронять цикл, ни возвращать апдейт."""
    store = DiaryStore()
    bot = _bot(store)
    sent = []
    asked_offsets = []
    stop = threading.Event()

    def fake_call(method, **params):
        asked_offsets.append(params.get("offset"))
        if len(asked_offsets) > 1:
            stop.set()
            return {"ok": True, "result": []}
        return {"ok": True, "result": [_message("сломай меня", update_id=10),
                                       _message("Пил кофе, с утра тревога", update_id=11)]}

    def fake_handle(message):
        if message.get("text") == "сломай меня":
            raise RuntimeError("разбор упал")
        return "порядок"

    bot._call = fake_call
    bot.handle = fake_handle
    bot.send = lambda chat_id, text: sent.append(text)

    bot.run(stop=stop)

    assert [t for t in sent if "порядок" in t]          # второе сообщение обработано
    assert any("Не получилось разобрать" in t for t in sent)
    assert asked_offsets == [0, 12]                     # оба апдейта подтверждены


def test_polling_reports_conflict_instead_of_dying():
    """Если тот же бот опрашивается ещё где-то, Telegram отвечает 409."""
    bot = _bot()
    stop = threading.Event()
    troubles = []

    def fake_call(method, **params):
        stop.set()
        raise TelegramError("getUpdates: 409 Conflict: terminated by other getUpdates")

    bot._call = fake_call
    bot.run(stop=stop, on_error=troubles.append)
    assert troubles and "409" in troubles[0]


# ── напоминания ───────────────────────────────────────────────────────────

def test_reminder_goes_to_the_one_who_kept_quiet():
    bot = _bot()
    bot.handle({"chat": {"id": 555}, "text": "/помощь"})    # чат появился в расписании
    sent = []
    bot.send = lambda chat_id, text: sent.append((chat_id, text))

    assert bot.remind(_evening(21)) == [555]
    assert len(sent) == 1 and "Как прошёл день" in sent[0][1]


def test_no_reminder_to_the_one_who_already_wrote_today():
    """Напоминание после записи выглядит так, будто мы её не услышали."""
    bot = _bot()
    bot.handle({"chat": {"id": 555}, "text": "Пил кофе, с утра тревога"})
    sent = []
    bot.send = lambda chat_id, text: sent.append(text)

    assert bot.remind(_evening(21)) == []
    assert sent == []


def test_reminder_is_not_repeated_the_same_evening():
    bot = _bot()
    bot.handle({"chat": {"id": 555}, "text": "/помощь"})
    bot.send = lambda chat_id, text: None

    assert bot.remind(_evening(21)) == [555]
    assert bot.remind(_evening(22)) == []


def test_reminder_time_is_changed_by_command():
    bot = _bot()
    answer = bot.handle({"chat": {"id": 555}, "text": "/напоминание 20:30"})
    assert "20:30" in answer
    assert bot.reminders.time_of(555) == "20:30"

    assert "не понял" in bot.handle({"chat": {"id": 555},
                                     "text": "/напоминание вечерком"}).lower()

    bot.handle({"chat": {"id": 555}, "text": "/напоминание выкл"})
    assert not bot.reminders.is_on(555)
    bot.send = lambda chat_id, text: None
    assert bot.remind(_evening(22)) == []


def test_failed_reminder_does_not_stop_the_rest():
    bot = _bot()
    for chat_id in (555, 556):
        bot.handle({"chat": {"id": chat_id}, "text": "/помощь"})

    def picky_send(chat_id, text):
        if chat_id == 555:
            raise TelegramError("sendMessage: 403 bot was blocked by the user")

    bot.send = picky_send
    assert bot.remind(_evening(21)) == [556]
