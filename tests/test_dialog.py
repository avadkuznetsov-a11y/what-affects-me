"""Один шаг разговора: он же на странице, он же в чате."""
from datetime import date, timedelta

from wam import dialog
from wam.diary import DiaryStore
from wam.insights import Link
from wam.schema import DayRecord, Fact

# Разборщик задаём явно: иначе на машине с ключом модели в окружении тест
# полезет в сеть и станет непредсказуемым.
RULES = dialog.Parser()


def _step(diary, text, **kwargs):
    return dialog.step(diary, text, parser=RULES, **kwargs)


def _link(metric: str, factor: str = "кофе") -> Link:
    """Готовая связь - из-за неё разговор спрашивает именно про этот показатель."""
    return Link(factor=factor, metric=metric, lag_days=0, effect=-1.5,
                days_with=10, days_without=10, p_value=0.01, p_adjusted=0.02)


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
    # «на 7» про силу тревоги - это плохой день: внутри шкала «больше значит
    # лучше», поэтому 7 названных превращаются в 3 записанных.
    assert diary.today().metric("тревога") == 3.0
    assert diary.pending is None


def test_reply_can_be_an_answer_and_a_story_at_once():
    """
    «на 3, вымотан после зала» - это и оценка сил, и тренировка. Раньше
    приходилось выбирать: записывалась только тренировка, а оценка пропадала,
    и второй раз про силы уже не спрашивали.
    """
    diary = DiaryStore().get("web")
    links = [_link("энергия")]
    _step(diary, "Пил кофе, настроение хорошее", links=links)
    assert "сил" in diary.pending                  # спросили именно про силы

    _step(diary, "на 3, вымотан после зала", links=links)
    assert diary.today().metric("энергия") == 3.0
    assert diary.today().factor("тренировка") == 1.0
    assert diary.today().metric("настроение") == 8.0    # сказанное раньше на месте


def test_answer_that_matches_the_guess_closes_the_question():
    """
    На «тревога какая-то» правила сами ставят 3.0. Человек отвечает «3» -
    значения совпали, и раньше ответ читался как молчание: тот же вопрос
    задавался снова и снова.
    """
    diary = DiaryStore().get("web")
    asked = _step(diary, "Тревога какая-то")[-1]["text"]

    messages = _step(diary, "3")
    assert diary.today().metric("тревога") == 7.0      # тревога на 3 - день спокойный
    assert diary.pending is None
    assert asked not in [m["text"] for m in messages]      # второй раз не спрашиваем


def test_long_reply_does_not_lose_the_question():
    """
    Реплика длиннее ответа - это рассказ, а не оценка. Вопрос при этом должен
    остаться: раньше он стирался, второй раз его не задавал asked, и оценка
    пропадала навсегда.
    """
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    question = diary.pending

    _step(diary, "на 7, но это скорее из-за того что вчера лёг рано")
    assert diary.pending == question            # вопрос всё ещё ждёт ответа

    _step(diary, "на 7")
    # «на 7» про силу тревоги - это плохой день: внутри шкала «больше значит
    # лучше», поэтому 7 названных превращаются в 3 записанных.
    assert diary.today().metric("тревога") == 3.0
    assert diary.pending is None


def test_the_same_question_is_not_asked_twice_in_a_row():
    """Заклинивший на одном вопросе бот - самая быстрая причина бросить дневник."""
    diary = DiaryStore().get("web")
    first = _step(diary, "Тревога какая-то")[-1]
    assert first["kind"] == "ask"

    for reply in ("3", "не знаю", "ну как-то так"):
        for message in _step(diary, reply):
            assert not (message["kind"] == "ask" and message["text"] == first["text"])


def test_greeting_does_not_repeat_the_day_and_the_question():
    """
    «Привет» после рассказа про день. Пока ответ выбирался по записи за весь
    день, бот перечислял в ответ все привычки с утра и второй раз задавал тот
    же вопрос - заказчик назвал это «несвязанным диалогом».
    """
    diary = DiaryStore().get("web")
    _step(diary, "Пил кофе часов в пять")
    question = diary.pending

    texts = [m["text"] for m in _step(diary, "привет")]
    assert "Записал:" not in texts               # эта фраза ничего не добавила
    assert question not in texts                 # и вопрос повторять незачем
    assert any("Здравствуйте" in t for t in texts)
    assert diary.pending == question             # вопрос не задан заново, но ждёт


def test_thanks_is_accepted():
    diary = DiaryStore().get("web")
    _step(diary, "Пил кофе часов в пять")

    texts = [m["text"] for m in _step(diary, "спасибо")]
    assert "Записал:" not in texts
    assert any("Пожалуйста" in t for t in texts)


def test_only_new_facts_are_repeated_back():
    """«Записал» - про то, что добавила эта фраза, а не весь день заново."""
    diary = DiaryStore().get("web")
    _step(diary, "Пил кофе часов в пять")

    note = next(m for m in _step(diary, "была тренировка, настроение хорошее")
                if m["text"] == "Записал:")["note"]
    assert "тренировка" in note and "настроение" in note
    assert "кофе" not in note                    # про кофе сказали в прошлой реплике


def test_the_hanging_question_is_not_asked_again_word_for_word():
    """
    Вопрос уже висит, а человек рассказал ещё одну привычку - слово в слово
    его не повторяем. Спросить про новую привычку при этом можно: это другой
    вопрос и про то, что человек сказал только что.
    """
    diary = DiaryStore().get("web")
    _step(diary, "Пил кофе часов в пять")
    question = diary.pending

    said = [m["text"] for m in _step(diary, "была тренировка")]
    assert question not in said
    assert diary.pending != question       # либо тот же висит, либо новый задан


def test_state_question_is_asked_once_a_day():
    """«А как вы себя чувствовали?» второй раз за разговор - это допрос."""
    diary = DiaryStore().get("web")
    _step(diary, "Пил кофе часов в пять")
    _step(diary, "ну как-то так")
    _step(diary, "была тренировка")
    _step(diary, "вечером пиво")

    asked = [m["text"] for m in diary.messages if m["kind"] == "ask"]
    assert asked.count(dialog.NO_STATE) <= 1


def test_the_conclusion_tail_is_not_repeated_word_for_word():
    """
    Хвост «Картина дня понятна...» с выводами печатался на каждую реплику
    подряд - на скриншоте заказчика он стоит дважды слово в слово. Теперь он
    звучит, только когда про названную привычку правда есть что сказать, и не
    повторяется на следующей фразе.
    """
    tail = "Картина дня понятна. Смотрю, что об этом говорит ваш дневник."
    diary = DiaryStore().get("web")
    for offset in range(1, 15):
        record = DayRecord(day=date.today() - timedelta(days=offset))
        record.add(Fact("factor", "кофе", 1.0 if offset % 2 else 0.0))
        record.add(Fact("metric", "тревога", 8.0 if offset % 2 else 3.0))
        diary.add(record)
    links = diary.links()
    assert links, "на таком дневнике связь кофе - тревога обязана найтись"

    # Первой репликой разговор уточняет детали привычки - сколько чашек кофе.
    # Выводы идут следующим шагом, когда уточнять уже нечего.
    first = [m["text"] for m in _step(diary, "Пил кофе, тревога 8 баллов", links=links)]
    assert any("кофе" in t.lower() and "?" in t for t in first)

    said = [m["text"] for m in _step(diary, "две чашки, последняя в пять", links=links)]
    assert tail in said

    again = [m["text"] for m in _step(diary, "спал 5 часов", links=links)]
    assert "Записал:" in again and tail not in again


def test_the_tail_is_silent_while_the_diary_is_short():
    """
    В первые дни выводов быть не может, и повторять это на каждую фразу -
    отписка. Пока дней мало, про отсутствие выводов молчим.
    """
    tail = "Картина дня понятна. Смотрю, что об этом говорит ваш дневник."
    diary = DiaryStore().get("web")
    said = [m["text"] for m in _step(diary, "Пил кофе, настроение хорошее")]
    assert tail not in said
    assert not any("выводов пока нет" in text for text in said)


def test_the_question_does_not_wait_forever():
    """
    Вопрос без срока ловил числа через много реплик: «выпил 2 кофе» становилось
    оценкой тревоги, которую человек называть не собирался.
    """
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    for reply in ("ну не знаю", "потом скажу", "ладно"):
        _step(diary, reply)

    assert diary.pending_question(date.today()) is None
    _step(diary, "на 7")
    assert diary.today().metric("тревога") == 3.0      # оценка мимо вопроса не идёт


def test_the_question_does_not_survive_the_day():
    """Ответ «на 7» назавтра приписал бы оценку дню, который человек не оценивал."""
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    question = diary.pending
    diary.expect(question, date.today() - timedelta(days=1))   # как будто спросили вчера

    _step(diary, "на 7")
    assert diary.today().metric("тревога") == 3.0
    assert diary.pending != question


def test_hedged_score_is_recorded():
    """«8 вроде» - это восьмёрка, а не мера чего-то: ответ терялся молча."""
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    _step(diary, "8 вроде")
    assert diary.today().metric("тревога") == 2.0   # названные 8 - тревожный день


def test_score_and_a_measure_in_one_reply():
    """«на 8, бегал 5 км» - это и оценка тревоги, и тренировка."""
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    _step(diary, "на 8, бегал 5 км")
    assert diary.today().metric("тревога") == 2.0   # названные 8 - тревожный день
    assert diary.today().factor("тренировка") == 1.0


def test_hours_in_the_answer_do_not_become_the_asked_score():
    """«спал 5 часов» в ответ про тревогу - это часы сна, а не тревога на пятёрку."""
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    assert diary.pending and diary.today().metric("тревога") == 3.0

    _step(diary, "спал 5 часов")
    assert diary.today().metric("качество сна") == 6.2
    assert diary.today().metric("тревога") == 3.0       # число ушло не в тревогу
    assert diary.pending                                # про силу тревоги спросят снова


def test_count_in_the_answer_does_not_become_the_asked_score():
    """«сегодня 8 встреч» в ответ про тревогу - это счёт встреч, а не восьмёрка."""
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    _step(diary, "сегодня 8 встреч")
    assert diary.today().metric("тревога") == 3.0


def test_hours_are_the_answer_to_the_question_about_sleep():
    """А вот на вопрос «сколько часов удалось поспать» часы - это и есть ответ."""
    diary = DiaryStore().get("web")
    links = [_link("качество сна")]
    _step(diary, "Пил кофе, настроение хорошее", links=links)
    assert "спалось" in diary.pending

    _step(diary, "спал 5 часов", links=links)
    assert diary.today().metric("качество сна") == 6.2


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


# ── пропущенные дни ───────────────────────────────────────────────────────

def _wrote_on(diary, day, habit="кофе"):
    """Как будто человек писал в дневник в этот день."""
    record = DayRecord(day=day)
    record.add(Fact("factor", habit, 1.0, "diary"))
    record.add(Fact("metric", "энергия", 6.0, "diary"))
    diary.timeline.add(record)


def test_asks_about_the_days_the_person_missed():
    diary = DiaryStore().get("web")
    _wrote_on(diary, date.today() - timedelta(days=3))

    messages = _step(diary, "Пил кофе, устал")
    asked = [m for m in messages if m["kind"] == "ask"]
    assert len(asked) == 1                      # один короткий вопрос, а не анкета
    assert "Вас не было два дня" in asked[0]["text"]


def test_the_gap_is_asked_about_once_a_day():
    """Человек вернулся в дневник, а его встречают допросом - так и уходят."""
    diary = DiaryStore().get("web")
    _wrote_on(diary, date.today() - timedelta(days=3))

    _step(diary, "Пил кофе, устал")
    again = _step(diary, "И ещё гулял вечером")
    assert not [m for m in again if "Вас не было" in m["text"]]


def test_no_gap_no_question():
    diary = DiaryStore().get("web")
    _wrote_on(diary, date.today() - timedelta(days=1))
    messages = _step(diary, "Пил кофе, устал")
    assert not [m for m in messages if "Вас не было" in m["text"]]


def test_story_about_yesterday_goes_into_yesterday():
    """«Вчера пил вино» - это факт про вчера, и в сегодняшний день он не ляжет."""
    diary = DiaryStore().get("web")
    yesterday = date.today() - timedelta(days=1)

    messages = _step(diary, "вчера пил вино")
    assert diary.record_for(yesterday).factor("алкоголь") == 1.0
    assert diary.today().factor("алкоголь") is None
    # Человек должен видеть, что его «вчера» мы поняли именно как вчера
    assert any(m["text"] == "Записал за вчера:" for m in messages)


def test_answer_with_a_day_inside_stays_todays_answer():
    """
    «на 7, но вчера лёг рано» - это оценка за сегодня. Пока висит вопрос,
    реплику во вчерашний день уводить нельзя.
    """
    diary = DiaryStore().get("web")
    _step(diary, "Тревога какая-то")
    _step(diary, "на 7, но это скорее из-за того что вчера лёг рано")
    assert diary.today().metric("тревога") is not None


# ── тяжёлые дни, вопросы к программе, поправки ────────────────────────────
#
# Всё это проверено живыми диалогами: на каждой строке ниже бот раньше отвечал
# так, что дневник хотелось закрыть.

def test_heavy_day_gets_condolences_not_a_question():
    diary = DiaryStore().get("web")
    messages = _step(diary, "у меня умер дедушка, весь день на похоронах")

    said = [m for m in messages if m["kind"] == "bot"]
    assert said and said[0]["text"] == dialog.HEAVY_REPLY
    assert not [m for m in messages if m["kind"] == "ask"]   # ни о чём не спрашиваем
    assert diary.today().factor("тяжёлое событие") == 1.0


def test_heavy_words_do_not_catch_ordinary_speech():
    """«Умеренно» и «сократили расходы» тяжёлым днём не считаются."""
    diary = DiaryStore().get("web")
    _step(diary, "пил умеренно, два бокала вина")
    assert diary.today().factor("тяжёлое событие") is None


def test_medical_question_gets_an_answer_even_with_facts():
    diary = DiaryStore().get("web")
    messages = _step(diary, "третий день болит голова, что мне выпить?")

    texts = [m["text"] for m in messages]
    assert dialog.MEDICAL_REPLY in texts
    assert diary.today().metric("головная боль") is not None


def test_question_to_us_is_not_an_answer_to_our_question():
    """«Что ты про меня знаешь?» - вопрос, а не ответ про количество."""
    diary = DiaryStore().get("web")
    _step(diary, "вечером выпил пива")
    messages = _step(diary, "что ты вообще про меня знаешь?")

    said = " ".join(m["text"] for m in messages if m["kind"] == "bot")
    assert "Дней в дневнике" in said
    assert "Записал" not in said


def test_annoyed_person_is_not_asked_again_today():
    diary = DiaryStore().get("web")
    _step(diary, "да достал ты со своими вопросами")
    messages = _step(diary, "пил кофе, спал часов пять")

    assert not [m for m in messages if m["kind"] == "ask"]
    assert diary.today().factor("кофе") == 1.0      # записываем всё равно


def test_privacy_and_delete_questions_are_answered():
    diary = DiaryStore().get("web")
    privacy = _step(diary, "а ты мои данные кому-нибудь передаёшь?")
    delete = _step(diary, "удали всё что записал")

    assert dialog.PRIVACY_REPLY in [m["text"] for m in privacy]
    assert dialog.DELETE_REPLY in [m["text"] for m in delete]


def test_doubt_about_method_is_answered():
    diary = DiaryStore().get("web")
    messages = _step(diary, "откуда ты знаешь что кофе виноват? может это совпадение")

    assert dialog.HOW_SURE_REPLY in [m["text"] for m in messages]
    assert diary.today().factor("кофе") is None     # это вопрос, а не запись дня


def test_bare_score_without_pending_question_asks_what_about():
    diary = DiaryStore().get("web")
    messages = _step(diary, "на 4")
    said = [m["text"] for m in messages]
    assert dialog.which_metric("на 4") in said
    assert "4 - это про что" in " ".join(said)      # число берём из реплики


def test_wrong_day_moves_the_record():
    diary = DiaryStore().get("web")
    today = date.today()
    _step(diary, "вчера был перелёт")
    assert diary.record_for(today - timedelta(days=1)).factor("перелёт") == 1.0

    messages = _step(diary, "а нет, вру, это было позавчера")

    said = " ".join(m["text"] for m in messages if m["kind"] == "bot")
    assert "Поправил" in said
    assert diary.record_for(today - timedelta(days=2)).factor("перелёт") == 1.0
    assert (today - timedelta(days=1)) not in [d.day for d in diary.timeline.days]


def test_ready_answers_do_not_repeat_word_for_word():
    """
    Три вопроса подряд про эксперимент давали один и тот же абзац три раза -
    так выглядит автоответчик, а не разговор. Второй раз отвечаем короче,
    третий - отсылкой к сказанному.
    """
    diary = DiaryStore().get("web")
    first = _step(diary, "а если я вообще брошу пить кофе на неделю?")
    second = _step(diary, "что будет если я перестану пить вечером?")
    third = _step(diary, "давай попробую неделю без сладкого")

    assert dialog.TRY_WITHOUT_REPLY in [m["text"] for m in first]
    assert dialog.SHORT_AGAIN[dialog.TRY_WITHOUT_REPLY] in [m["text"] for m in second]
    assert dialog.ALREADY_SAID in [m["text"] for m in third]
