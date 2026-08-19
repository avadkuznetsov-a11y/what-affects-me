"""Один шаг разговора: он же на странице, он же в чате."""
from wam import dialog
from wam.diary import DiaryStore

# Разборщик задаём явно: иначе на машине с ключом модели в окружении тест
# полезет в сеть и станет непредсказуемым.
RULES = dialog.Parser()


def _step(diary, text, **kwargs):
    return dialog.step(diary, text, parser=RULES, **kwargs)


def test_step_writes_down_facts():
    diary = DiaryStore().get("web")
    messages = _step(diary, "Пил кофе часов в пять, с утра тревога")

    assert messages[0]["text"].startswith("Пил кофе")     # эхо того, что сказал человек
    assert messages[0]["from"] == "page"
    written = next(m for m in messages if m["text"] == "Записал:")
    assert "кофе" in written["note"]
    assert diary.today().factor("кофе") == 1.0
    assert diary.today().metric("тревога") is not None


def test_unclear_phrase_gets_one_question():
    diary = DiaryStore().get("web")
    messages = _step(diary, "ну как-то так")
    asks = [m for m in messages if m["kind"] == "ask"]
    assert len(asks) == 1
    assert diary.pending == asks[0]["text"]


def test_answer_goes_into_the_same_day():
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    assert diary.pending                       # спросили, насколько сильная
    assert diary.today().metric("тревога") == 3.0

    _step(diary, "на 7")
    assert diary.today().metric("тревога") == 7.0
    assert diary.pending is None


def test_ring_readings_land_in_the_diary():
    diary = DiaryStore().get("web")
    _step(diary, "Пил кофе, чувствую себя разбитым",
          ring={"sleep_score": 40, "stress_level": 70, "steps": 3000})
    assert diary.today().metric("качество сна") == 4.0
    assert diary.today().factor("мало спал") == 1.0     # показание стало причиной


def test_chat_message_is_visible_on_the_page_and_back():
    """Дневник один: что пришло из чата, видно в ленте страницы с пометкой."""
    store = DiaryStore()
    store.bind(store.new_code("web"), 555)
    page = store.get("web")
    chat = store.diary_for_chat(555)
    assert chat is page

    _step(page, "Пил кофе, бодрый день")
    mark = page.seq
    _step(chat, "Была тренировка, настроение хорошее", origin="chat")

    fresh = page.feed(since=mark)
    assert fresh and all(m["from"] == "chat" for m in fresh)
    assert "тренировка" in " ".join(m.get("note", "") for m in fresh)
    # и наоборот: сказанное на странице лежит в том же дневнике
    assert page.today().factor("кофе") == 1.0
    assert chat.today().factor("тренировка") == 1.0
