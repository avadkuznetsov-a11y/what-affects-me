"""Общий дневник: один на страницу и на чат, с одноразовым кодом привязки."""
import re
import threading
import time
from datetime import date, timedelta

from wam.diary import (CODE_ALPHABET, CODE_LIFETIME, CODE_TRIES_PER_CHAT,
                       CODE_TRIES_TOTAL, GUEST_DIARIES, Diary, DiaryStore, looks_like_code)
from wam.questions import apply_answer
from wam.schema import DayRecord, Fact


def test_one_key_gives_one_diary():
    store = DiaryStore()
    first = store.get("web")
    first.today().add(Fact("factor", "кофе"))
    assert store.get("web") is first
    assert store.get("web").today().factor("кофе") == 1.0


def test_code_looks_like_promised():
    code = DiaryStore().new_code("web")
    assert re.fullmatch(r"[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}", code)
    assert not (set("O0I1L") & set(code))       # похожих знаков в коде нет
    assert looks_like_code(code) and looks_like_code(" " + code.lower() + " ")
    assert not looks_like_code("пил кофе часов в пять")


def test_code_is_not_worth_guessing():
    """Четыре цифры подбирались за вечер: теперь вариантов на сорок бит."""
    codes = {DiaryStore().new_code("web") for _ in range(200)}
    assert len(codes) == 200
    assert len(CODE_ALPHABET) ** 8 > 10 ** 11


def test_code_is_read_as_written_by_hand():
    store = DiaryStore()
    code = store.new_code("web")
    assert store.bind(f" {code.replace('-', ' ').lower()} ", 555) == "web"


def test_code_works_once():
    store = DiaryStore()
    code = store.new_code("web")
    assert store.bind(code, 555) == "web"
    assert store.bind(code, 777) is None       # второй раз тем же кодом нельзя


def test_stale_code_is_refused():
    store = DiaryStore()
    code = store.new_code("web")
    # Не ждём четверть часа: сдвигаем время выдачи в прошлое
    store._codes[code] = (store._codes[code][0], time.time() - CODE_LIFETIME - 1)
    assert store.bind(code, 555) is None


def test_someone_elses_code_is_refused():
    store = DiaryStore()
    store.new_code("web")
    assert store.bind("AAAA-BBBB", 555) is None


def test_guessing_the_code_runs_into_a_pause():
    """Подбор кода: с одного чата - пауза, со всех сразу - код гасим."""
    store = DiaryStore()
    code = store.new_code("web")

    for _ in range(CODE_TRIES_PER_CHAT):
        assert store.bind("AAAA-BBBB", 555) is None
    assert store.code_paused(555)
    assert store.bind(code, 555) is None        # в паузе не годится и верный код
    assert not store.code_paused(777)           # соседний чат не наказан

    # Пауза не вечная: время вышло - чат снова может привязаться
    store._misses[555] = (CODE_TRIES_PER_CHAT, time.time() - 1)
    assert not store.code_paused(555)
    assert store.bind(code, 555) == "web"


def test_many_chats_guessing_burn_the_code():
    store = DiaryStore()
    code = store.new_code("web")
    for chat in range(CODE_TRIES_TOTAL):
        store.bind("AAAA-BBBB", 1000 + chat)
    assert store.bind(code, 42) is None         # прежний код погашен
    assert store.bind(store.new_code("web"), 42) == "web"


def test_binding_brings_over_what_was_written_in_the_chat():
    """Человек писал боту до привязки - эти дни не должны пропасть."""
    store = DiaryStore()
    chat = store.diary_for_chat(555)
    yesterday = DayRecord(day=date.today() - timedelta(days=1))
    yesterday.add(Fact("factor", "кофе"))
    chat.add(yesterday)
    chat.today().add(Fact("metric", "тревога", 6.0))

    store.bind(store.new_code("web"), 555)

    page = store.get("web")
    assert [d.day for d in page.timeline.days] == [yesterday.day, date.today()]
    assert page.today().metric("тревога") == 6.0
    assert store.diary_for_chat(555) is page


def test_unbound_diaries_do_not_pile_up_forever():
    store = DiaryStore()
    for chat in range(GUEST_DIARIES + 10):
        store.diary_for_chat(chat)
    assert len(store._diaries) <= GUEST_DIARIES


def test_disconnect_unlinks_the_chats():
    store = DiaryStore()
    store.bind(store.new_code("web"), 555)
    assert store.unlink("web") == [555]
    assert store.key_for_chat(555) == "tg:555"


def test_one_chat_can_be_unlinked_without_touching_the_rest():
    """Отвязали один чат - остальные пишут дальше, а записи никуда не делись."""
    store = DiaryStore()
    store.bind(store.new_code("web"), 555)
    store.bind(store.new_code("web"), 777)
    store.get("web").add(DayRecord(day=date.today()))

    assert store.unlink_chat(555, "web") is True
    assert store.unlink_chat(555, "web") is False   # второй раз отвязывать нечего
    assert store.linked_chats("web") == [777]
    assert store.key_for_chat(555) == "tg:555"
    assert store.get("web").timeline.days       # записи остались у страницы


def test_a_chat_of_another_diary_is_not_unlinked():
    """Отвязать можно только свой чат: чужой номер чужую привязку не рвёт."""
    store = DiaryStore()
    store.bind(store.new_code("web"), 555)
    assert store.unlink_chat(555, "tg:999") is False
    assert store.linked_chats("web") == [555]


def test_code_is_live_only_while_the_store_answers_to_it():
    """
    Свежесть кода знает хранилище: код гаснет и по времени, и от перебора из
    многих чатов. Тот, кто код выдал, по своим часам этого не увидит.
    """
    store = DiaryStore()
    code = store.new_code("web")
    assert store.code_is_live(code)
    assert store.code_is_live(code.replace("-", " ").lower())   # как записан - неважно
    assert not store.code_is_live("AAAA-BBBB")

    for chat in range(CODE_TRIES_TOTAL):
        store.bind("AAAA-BBBB", 1000 + chat)
    assert not store.code_is_live(code)          # код погашен перебором

    code = store.new_code("web")
    store._codes[code] = (store._codes[code][0], time.time() - CODE_LIFETIME - 1)
    assert not store.code_is_live(code)          # и просроченный тоже не живой


def test_unbound_chat_gets_its_own_diary():
    store = DiaryStore()
    assert store.key_for_chat(555) == "tg:555"
    assert store.diary_for_chat(555) is not store.get("web")

    store.bind(store.new_code("web"), 555)
    assert store.key_for_chat(555) == "web"
    assert store.diary_for_chat(555) is store.get("web")


def test_feed_gives_only_new_messages():
    diary = Diary("web")
    diary.say("me", "привет")
    mark = diary.seq
    diary.say("bot", "записал", note="кофе", origin="chat")
    fresh = diary.feed(since=mark)
    assert [m["text"] for m in fresh] == ["записал"]
    assert fresh[0]["from"] == "chat" and fresh[0]["seq"] == mark + 1


def test_reset_keeps_earlier_days():
    diary = Diary("web")
    yesterday = DayRecord(day=date.today() - timedelta(days=1))
    yesterday.add(Fact("factor", "кофе"))
    diary.add(yesterday)
    diary.today().add(Fact("factor", "тренировка"))
    diary.say("me", "привет")

    diary.reset()
    assert diary.messages == [] and diary.pending is None
    assert [d.day for d in diary.timeline.days] == [yesterday.day]


def test_links_are_counted_once_per_change():
    diary = Diary("web")
    for offset in range(8):
        record = DayRecord(day=date.today() - timedelta(days=offset))
        record.add(Fact("factor", "кофе"))
        record.add(Fact("metric", "тревога", 5.0))
        diary.add(record)
    first = diary.links()
    assert diary.links() is first          # ничего не изменилось - считать нечего
    diary.today().add(Fact("metric", "энергия", 4.0))
    assert diary.links() is not first      # появился факт - пересчитали


def test_answer_to_a_question_drops_the_cached_links():
    """Ответ заменяет оценку: фактов столько же, а связи считать заново."""
    diary = Diary("web")
    for offset in range(8):
        record = DayRecord(day=date.today() - timedelta(days=offset))
        record.add(Fact("factor", "кофе"))
        record.add(Fact("metric", "тревога", 3.0))
        diary.add(record)
    first = diary.links()

    apply_answer(diary.today(), "Насколько сильной была тревога, от 0 до 10?", "на 9")
    assert diary.today().metric("тревога") == 1.0   # названные 9 - очень тревожный день
    assert diary.links() is not first


def test_many_threads_write_into_one_diary():
    """Страница и бот пишут в один дневник одновременно - терять факты нельзя."""
    store = DiaryStore()
    diary = store.get("web")

    def work(number: int) -> None:
        for step in range(10):
            with diary.lock:
                diary.today().add(Fact("factor", f"привычка {number}-{step}"))
                diary.say("me", f"сообщение {number}-{step}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(diary.today().facts) == 200
    assert len(diary.messages) == 200
    assert len({m["seq"] for m in diary.messages}) == 200
