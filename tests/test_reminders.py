"""Расписание напоминаний: вечером, не чаще раза в день и не всем подряд."""
from datetime import date, datetime

import pytest

from bot.reminders import DEFAULT_TIME, LATEST_HOUR, MAX_CHATS, Schedule, minutes_of

CHAT = 555
DAY = date(2026, 8, 20)


def _at(hour, minute=0):
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute)


def test_time_is_read_in_the_forms_people_write_it():
    assert minutes_of("21:00") == 21 * 60
    assert minutes_of("9.30") == 9 * 60 + 30
    assert minutes_of("7") == 7 * 60
    for wrong in ("", "вечером", "25:00", "21:70"):
        with pytest.raises(ValueError):
            minutes_of(wrong)


def test_reminder_waits_for_its_hour():
    schedule = Schedule("21:00")
    schedule.remember(CHAT)
    assert not schedule.due(CHAT, _at(20, 59))
    assert schedule.due(CHAT, _at(21, 0))


def test_only_one_reminder_a_day():
    """Второе напоминание за вечер - самый быстрый способ, чтобы бота заглушили."""
    schedule = Schedule("21:00")
    schedule.remember(CHAT)
    assert schedule.due(CHAT, _at(21, 0))
    schedule.mark_sent(CHAT, DAY)
    assert not schedule.due(CHAT, _at(22, 0))


def test_deep_night_stays_quiet():
    """Программу могли не запускать весь вечер - будить человека мы не станем."""
    schedule = Schedule("21:00")
    schedule.remember(CHAT)
    assert schedule.due(CHAT, _at(LATEST_HOUR - 1, 59))
    assert not schedule.due(CHAT, _at(LATEST_HOUR, 0))


def test_time_can_be_changed_and_turned_off():
    schedule = Schedule()
    schedule.remember(CHAT)
    assert schedule.time_of(CHAT) == DEFAULT_TIME

    assert schedule.set_time(CHAT, "20:30") == "20:30"
    assert not schedule.due(CHAT, _at(20, 29))
    assert schedule.due(CHAT, _at(20, 30))

    schedule.turn_off(CHAT)
    assert not schedule.is_on(CHAT)
    assert schedule.time_of(CHAT) == ""
    assert not schedule.due(CHAT, _at(23, 0))

    assert schedule.turn_on(CHAT) == "20:30"      # прежнее время помним
    assert schedule.due(CHAT, _at(21, 0))


def test_only_those_who_wrote_are_in_the_schedule():
    """Рассылать незнакомым чатам некому и незачем."""
    schedule = Schedule()
    assert schedule.chats() == []
    schedule.remember(CHAT)
    schedule.remember(CHAT)
    assert schedule.chats() == [CHAT]


def test_schedule_does_not_grow_forever():
    """Боту пишет кто угодно, а память у прототипа общая на всех."""
    schedule = Schedule()
    for chat_id in range(MAX_CHATS + 5):
        schedule.remember(chat_id)
    chats = schedule.chats()
    assert len(chats) == MAX_CHATS
    assert 0 not in chats and MAX_CHATS + 4 in chats
