"""Голосовые сообщения: путь «голос → текст → факты» без сети."""
from datetime import date

from wam.extract import RuleExtractor
from wam.voice import OfflineSpeech, transcribe_voice


def test_voice_turns_into_facts():
    text = transcribe_voice(b"fake audio", "ogg",
                            OfflineSpeech("Сходил в зал, вечером бодрый"))
    record = RuleExtractor().extract(text, date(2026, 8, 1))
    assert record.factor("тренировка") == 1.0


def test_unknown_format_does_not_crash():
    """Мессенджер прислал что-то странное — продукт просит написать текстом, но живёт."""
    assert transcribe_voice(b"...", "flac", OfflineSpeech("текст")) == ""


def test_failed_recognition_returns_empty():
    class Broken:
        def transcribe(self, audio, fmt="ogg"):
            raise RuntimeError("сервис недоступен")

    assert transcribe_voice(b"...", "ogg", Broken()) == ""
