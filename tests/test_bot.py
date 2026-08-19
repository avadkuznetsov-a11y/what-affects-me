"""Бот: привязка чата, разбор сообщений и стойкость цикла опроса."""
import threading

from bot.telegram import TelegramBot, TelegramError, token_is_shaped_right
from wam import dialog
from wam.diary import DiaryStore

# Токен ненастоящий, но правильной формы: настоящих в репозитории быть не должно.
FAKE_TOKEN = "1234567:" + "a" * 35


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
    answer = bot.handle(_message("1234-МИРА")["message"])
    assert "не подошёл" in answer


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
