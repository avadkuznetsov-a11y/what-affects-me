"""
Дневник на диске: что человек записал, то и осталось после перезапуска.

Перезапуск программы тут - это новый `DiaryStore` на тот же файл: другой
процесс поднимать незачем, всё, что переживает перезапуск, читается при
создании хранилища.

Все тесты работают во временном каталоге: писать в настоящий дневник человека
(`wam.storage.DIARY_FILE`) тесты не должны никогда.
"""
import sqlite3
import stat
from datetime import date, timedelta

from wam.diary import Diary, DiaryStore
from wam.schema import DayRecord, Fact
from wam.storage import Storage


def test_a_day_survives_a_restart(tmp_path):
    """Главное обещание: закрыли программу - записи на месте."""
    path = tmp_path / "diary.db"
    store = DiaryStore(path)
    diary = store.get("web")
    yesterday = date.today() - timedelta(days=1)
    record = DayRecord(day=yesterday, raw_text="вчера пил вино, спал плохо")
    record.add(Fact("factor", "алкоголь", 1.0, quote="пил вино"))
    record.add(Fact("metric", "качество сна", 3.0))
    diary.add(record)
    diary.city = "Москва"
    diary.save()

    again = DiaryStore(path).get("web")
    assert [d.day for d in again.timeline.days] == [yesterday]
    kept = again.timeline.days[0]
    assert kept.raw_text == "вчера пил вино, спал плохо"
    assert kept.factor("алкоголь") == 1.0
    assert kept.metric("качество сна") == 3.0
    assert kept.facts[0].quote == "пил вино"
    assert again.city == "Москва"


def test_facts_of_one_day_are_kept_whole(tmp_path):
    """Дописали к тому же дню - на диске день целиком, а не последний факт."""
    path = tmp_path / "diary.db"
    diary = DiaryStore(path).get("web")
    diary.today().add(Fact("factor", "кофе", 2.0))
    diary.save()
    diary.today().add(Fact("metric", "тревога", 4.0))
    diary.save()

    again = DiaryStore(path).get("web")
    assert again.today().factor("кофе") == 2.0
    assert again.today().metric("тревога") == 4.0


def test_links_are_counted_over_the_saved_days(tmp_path):
    """Связи после перезапуска считаются по поднятым с диска дням."""
    path = tmp_path / "diary.db"
    diary = DiaryStore(path).get("web")
    # Четырнадцати дней связь не наберёт: в каждой группе нужно по семь
    for offset in range(20):
        record = DayRecord(day=date.today() - timedelta(days=offset))
        record.add(Fact("factor", "кофе", 1.0 if offset % 2 else 0.0))
        record.add(Fact("metric", "тревога", 3.0 if offset % 2 else 8.0))
        diary.add(record)
    diary.save()

    again = DiaryStore(path).get("web")
    assert len(again.timeline) == 20
    assert any(link.factor == "кофе" for link in again.links())


def test_the_chat_stays_bound_after_a_restart(tmp_path):
    """Без этого человек в своём же чате становится чужим и пишет в пустоту."""
    path = tmp_path / "diary.db"
    store = DiaryStore(path)
    code = store.new_code("web")
    assert store.bind(code, 777) == "web"
    store.diary_for_chat(777).today().add(Fact("factor", "тренировка"))
    store.diary_for_chat(777).save()

    again = DiaryStore(path)
    assert again.key_for_chat(777) == "web"
    assert again.get("web").today().factor("тренировка") == 1.0


def test_an_unlinked_chat_stays_unlinked(tmp_path):
    """Человек закрыл доступ чату - перезапуск не должен его вернуть."""
    path = tmp_path / "diary.db"
    store = DiaryStore(path)
    store.bind(store.new_code("web"), 777)
    assert store.unlink_chat(777, "web")

    assert DiaryStore(path).key_for_chat(777) == "tg:777"


def test_days_written_from_a_chat_move_to_the_page_diary(tmp_path):
    """Привязка переливает записи чата на страницу - и на диске тоже."""
    path = tmp_path / "diary.db"
    store = DiaryStore(path)
    guest = store.diary_for_chat(777)
    guest.today().add(Fact("factor", "кофе"))
    guest.save()
    store.bind(store.new_code("web"), 777)

    again = DiaryStore(path)
    assert again.get("web").today().factor("кофе") == 1.0
    # Прежний дневник чата не остался лежать вторым экземпляром тех же дней
    assert again.get("tg:777").timeline.days == []


def test_the_conversation_starts_over(tmp_path):
    """Лента, вопрос и коды - состояние разговора, а не дневник: их не храним."""
    path = tmp_path / "diary.db"
    store = DiaryStore(path)
    diary = store.get("web")
    diary.today().add(Fact("factor", "кофе"))
    diary.say("me", "пил кофе")
    diary.expect("Как спалось?", date.today())
    code = store.new_code("web")
    diary.save()

    again = DiaryStore(path)
    assert again.get("web").messages == []
    assert again.get("web").pending is None
    assert again.get("web").seq == 0
    assert not again.code_is_live(code)
    assert again.get("web").today().factor("кофе") == 1.0


def test_reset_forgets_today_on_disk_too(tmp_path):
    """«Начать заново» должно пережить перезапуск, иначе оно ничего не значит."""
    path = tmp_path / "diary.db"
    diary = DiaryStore(path).get("web")
    diary.today().add(Fact("factor", "кофе"))
    yesterday = DayRecord(day=date.today() - timedelta(days=1))
    yesterday.add(Fact("factor", "тренировка"))
    diary.add(yesterday)
    diary.save()
    diary.reset()

    again = DiaryStore(path).get("web")
    assert [d.day for d in again.timeline.days] == [date.today() - timedelta(days=1)]


def test_a_clean_start_needs_no_file(tmp_path):
    """У того, кто запустил прототип впервые, файла ещё нет - и это не ошибка."""
    path = tmp_path / "diary.db"
    diary = DiaryStore(path).get("web")
    assert diary.timeline.days == []
    assert diary.city == ""
    diary.today().add(Fact("factor", "кофе"))
    diary.save()
    assert path.exists()


def test_only_the_owner_can_read_the_diary(tmp_path):
    """Дневник - самое личное, что есть у человека: права только владельцу."""
    path = tmp_path / "diary.db"
    DiaryStore(path).get("web").save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_broken_file_does_not_break_the_start(tmp_path):
    """Лучше пустой дневник и строка в логе, чем программа, которая не встаёт."""
    path = tmp_path / "diary.db"
    path.write_bytes("\x00\x01 это не база данных, а обрывок записи".encode())

    store = DiaryStore(path)
    diary = store.get("web")
    assert diary.timeline.days == []
    # Испорченный файл отложен, а не стёрт: это чужие записи
    assert (tmp_path / "diary.db.broken").exists()

    # И дальше дневник работает: новые записи ложатся в свежий файл
    diary.today().add(Fact("factor", "кофе"))
    diary.save()
    assert DiaryStore(path).get("web").today().factor("кофе") == 1.0


def test_a_half_written_file_does_not_break_the_start(tmp_path):
    """Программу убили посреди записи - от файла остался обрубок."""
    path = tmp_path / "diary.db"
    diary = DiaryStore(path).get("web")
    diary.today().add(Fact("factor", "кофе"))
    diary.save()
    whole = path.read_bytes()
    path.write_bytes(whole[:len(whole) // 3])

    assert DiaryStore(path).get("web").timeline.days == []


def test_a_diary_without_a_file_lives_in_memory(tmp_path):
    """Так заведены все остальные тесты: настоящий дневник они не трогают."""
    store = DiaryStore()
    store.get("web").today().add(Fact("factor", "кофе"))
    store.get("web").save()          # молча ничего не делает
    assert list(tmp_path.iterdir()) == []


def test_only_the_changed_day_is_written(tmp_path, monkeypatch):
    """
    На диск за реплику уходит один день, а не вся история: дневник пишется на
    каждое слово человека, и переписывать полгода записей ради него нельзя.
    """
    path = tmp_path / "diary.db"
    store = DiaryStore(path)
    diary = store.get("web")
    for offset in range(30):
        record = DayRecord(day=date.today() - timedelta(days=offset))
        record.add(Fact("factor", "кофе"))
        diary.add(record)
    diary.save()

    written = []
    storage = store._storage
    was = storage._write_day

    def spy(key, record):
        written.append(record.day)
        was(key, record)

    monkeypatch.setattr(storage, "_write_day", spy)

    diary.save()                                     # ничего не изменилось
    assert written == []
    diary.today().add(Fact("metric", "тревога", 5.0))
    diary.save()
    assert written == [date.today()]


def test_a_day_with_a_broken_date_is_skipped(tmp_path):
    """Одна испорченная строка не стоит потерянного дневника."""
    path = tmp_path / "diary.db"
    diary = DiaryStore(path).get("web")
    diary.today().add(Fact("factor", "кофе"))
    diary.save()

    db = sqlite3.connect(path)
    with db:
        db.execute("INSERT INTO days(diary, day, raw_text) VALUES('web', 'позавчера', '')")
    db.close()

    again = DiaryStore(path).get("web")
    assert [d.day for d in again.timeline.days] == [date.today()]


def test_a_diary_keeps_writing_when_the_file_is_gone(tmp_path):
    """Файл забрали из-под программы - разговор из-за этого не рвётся."""
    path = tmp_path / "diary.db"
    storage = Storage(path)
    diary = Diary("web", storage)
    diary.today().add(Fact("factor", "кофе"))
    storage.close()                                  # как будто писать больше некуда
    diary.today().add(Fact("metric", "тревога", 5.0))
    diary.save()                                     # молча жалуется в лог, но не падает
    assert diary.today().metric("тревога") == 5.0
