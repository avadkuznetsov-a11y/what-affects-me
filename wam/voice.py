"""
Голосовые сообщения.

Основной способ ввода в продукте — надиктовать пару фраз в мессенджер, а не
печатать. Голосовое приходит файлом, мы переводим его в текст и дальше
работаем как с обычной записью.

Здесь только адаптеры распознавания. Для российского контура первым идёт
SaluteSpeech, тот же Сбер, что и кольцо с GigaChat. Ключи берутся из
окружения, в репозитории их нет.
"""
from __future__ import annotations

import json
import os
from typing import Callable, Protocol

# Мессенджеры отдают голосовые в разных форматах, поэтому сразу оговариваем,
# что принимаем: ogg/opus (Telegram), m4a (iOS), wav.
SUPPORTED = {"ogg", "oga", "opus", "m4a", "mp3", "wav"}


class SpeechToText(Protocol):
    """Любой распознаватель речи."""

    def transcribe(self, audio: bytes, fmt: str = "ogg") -> str: ...


class OfflineSpeech:
    """
    Заглушка для разработки и тестов: возвращает заранее заданный текст.
    Нужна, чтобы весь путь «голос → факты → связи» можно было прогнать
    без единого сетевого вызова.
    """

    def __init__(self, text: str = "") -> None:
        self._text = text

    def transcribe(self, audio: bytes, fmt: str = "ogg") -> str:
        if fmt not in SUPPORTED:
            raise ValueError(f"формат {fmt} не поддерживается")
        return self._text


class SaluteSpeech:
    """
    Распознавание речи SaluteSpeech. Токен читается из SALUTE_SPEECH_TOKEN.
    Сетевой вызов вынесен в отдельный метод, чтобы его было легко подменить
    в тестах и чтобы пакет работал без установленных http-библиотек.
    """

    URL = "https://smartspeech.sber.ru/rest/v1/speech:recognition"

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("SALUTE_SPEECH_TOKEN", "")

    def transcribe(self, audio: bytes, fmt: str = "ogg") -> str:
        if fmt not in SUPPORTED:
            raise ValueError(f"формат {fmt} не поддерживается")
        if not self._token:
            raise RuntimeError("не задан SALUTE_SPEECH_TOKEN")

        import urllib.request

        content_type = "audio/ogg;codecs=opus" if fmt in {"ogg", "oga", "opus"} else f"audio/{fmt}"
        request = urllib.request.Request(
            self.URL,
            data=audio,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": content_type},
        )
        from .llm import ssl_context

        with urllib.request.urlopen(request, timeout=60, context=ssl_context()) as response:
            payload = json.loads(response.read())
        return " ".join(payload.get("result", [])).strip()


def transcribe_voice(audio: bytes, fmt: str = "ogg",
                     engine: SpeechToText | None = None) -> str:
    """
    Перевести голосовое в текст. Если распознавание не сработало, возвращаем
    пустую строку: продукт попросит человека написать текстом, но не упадёт.
    """
    engine = engine or OfflineSpeech()
    try:
        return engine.transcribe(audio, fmt)
    except Exception:
        return ""
