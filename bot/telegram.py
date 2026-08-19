"""
Мира в Telegram: основной способ вести дневник.

Человек пишет или наговаривает пару фраз, бот разбирает их, показывает, что
понял, и говорит, что об этих привычках уже известно. Раз в неделю можно
попросить разбор целиком.

Записи лежат в общем дневнике (wam.diary): если открыта страница Миры и человек
прислал боту код привязки, чат и страница ведут один дневник на двоих.

Запуск:
    export TELEGRAM_TOKEN=...          # токен от @BotFather
    export SALUTE_SPEECH_TOKEN=...     # необязательно, для голосовых
    python3 -m bot.telegram

Сторонних библиотек нет: только стандартная библиотека, чтобы запускалось
где угодно без установки зависимостей.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from wam import dialog
from wam.diary import DiaryStore, looks_like_code
from wam.phrases import say
from wam.voice import SaluteSpeech, transcribe_voice

API = "https://api.telegram.org"
TIMEOUT = 30
MAX_FILE = 20 * 1024 * 1024      # больше Telegram и не отдаёт, а память не резиновая

# Токен от @BotFather выглядит всегда одинаково: номер бота, двоеточие, ключ.
# Проверяем форму до обращения к сети, чтобы не гонять запрос впустую.
TOKEN_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")

HELP = (
    "Привет, я Мира - дневник, который ищет причины.\n\n"
    "Расскажите, как прошёл день - обычными словами или голосовым.\n\n"
    "Например: «пил кофе часов в пять, спал часов пять, с утра тревога».\n\n"
    "Я запомню, что было и как вы себя чувствовали. Когда наберётся достаточно "
    "дней, скажу, что на вас влияет.\n\n"
    "Если у вас открыта страница Миры, пришлите мне код привязки с неё - "
    "и записи из чата и со страницы будут в одном дневнике.\n\n"
    "/итоги - что уже известно\n"
    "/помощь - это сообщение"
)


class TelegramError(RuntimeError):
    """Телеграм ответил отказом или не ответил совсем."""


def token_is_shaped_right(token: str) -> bool:
    return bool(TOKEN_SHAPE.fullmatch((token or "").strip()))


class TelegramBot:
    def __init__(self, token: str | None = None, store: DiaryStore | None = None,
                 parser: dialog.Parser | None = None, timeout: int = TIMEOUT) -> None:
        self.token = (token or os.environ.get("TELEGRAM_TOKEN", "")).strip()
        if not self.token:
            raise RuntimeError("не задан TELEGRAM_TOKEN")
        self.store = store or DiaryStore()
        self.parser = parser
        self.timeout = timeout
        self.speech = SaluteSpeech() if os.environ.get("SALUTE_SPEECH_TOKEN") else None

    # ── общение с Telegram ────────────────────────────────────────────────
    def _hide(self, text: str) -> str:
        """
        Токен нельзя показывать никому: urllib кладёт в текст ошибки полный
        адрес запроса, а в адресе токен. Всё, что уходит в лог или человеку,
        проходит через эту замену.
        """
        return text.replace(self.token, "***") if self.token else text

    def _call(self, method: str, **params) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=self.timeout + 5) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = json.loads(exc.read()).get("description", "")
            except Exception:
                pass
            raise TelegramError(self._hide(f"{method}: {exc.code} {body or exc.reason}")) from None
        except urllib.error.URLError as exc:
            raise TelegramError(self._hide(f"{method}: нет связи ({exc.reason})")) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramError(f"{method}: непонятный ответ") from None

        if not isinstance(payload, dict) or not payload.get("ok"):
            reason = payload.get("description", "отказ") if isinstance(payload, dict) else "отказ"
            raise TelegramError(self._hide(f"{method}: {reason}"))
        return payload

    def username(self) -> str:
        """Имя бота по токену. Заодно проверка, что токен рабочий."""
        return self._call("getMe")["result"].get("username", "")

    def send(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", chat_id=chat_id, text=text)

    def download(self, file_id: str) -> bytes:
        info = self._call("getFile", file_id=file_id)
        path = info["result"]["file_path"]
        with urllib.request.urlopen(f"{API}/file/bot{self.token}/{path}",
                                    timeout=self.timeout) as r:
            return r.read(MAX_FILE)

    # ── обработка сообщений ───────────────────────────────────────────────
    def handle(self, message: dict) -> str:
        """Разобрать одно сообщение и вернуть ответ. Сети тут нет - удобно тестировать."""
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None:
            return ""       # без чата отвечать некому, и дневник заводить не на кого
        text = (message.get("text") or "").strip()

        if text.startswith("/start") or text.startswith("/помощь") or text.startswith("/help"):
            return HELP
        if text.startswith("/итоги") or text.startswith("/summary"):
            return self.summary(chat_id)
        if not text:
            return "Пока понимаю только текст и голосовые. Напишите пару фраз о дне."

        # Код привязки проверяем до разбора речи: в «4821-МИРА» разборщик
        # увидит ноль фактов и бот начнёт спрашивать про самочувствие.
        if looks_like_code(text):
            return self._bind(text, chat_id)

        diary = self.store.diary_for_chat(chat_id)
        # Связи считаем один раз за сообщение: раньше они считались трижды,
        # и на дневнике в месяц бот замолкал на десяток секунд.
        messages = dialog.step(diary, text, links=diary.links(),
                               parser=self.parser, origin="chat")
        return self._as_text(messages)

    def handle_voice(self, message: dict) -> str:
        """Голосовое: скачиваем, распознаём, дальше как обычный текст."""
        voice = message.get("voice") or message.get("audio") or {}
        file_id = voice.get("file_id")
        if not file_id:
            return "Не смог забрать голосовое, попробуйте текстом."
        if self.speech is None:
            return ("Распознавание голоса пока не подключено - напишите текстом. "
                    "Для голосовых нужен ключ SaluteSpeech.")
        audio = self.download(file_id)
        text = transcribe_voice(audio, "ogg", self.speech)
        if not text:
            return "Не разобрал речь. Попробуйте ещё раз или напишите текстом."
        return self.handle({**message, "text": text})

    def _bind(self, code: str, chat_id: int) -> str:
        if self.store.bind(code, chat_id) is None:
            return ("Такой код не подошёл: он мог устареть или уже быть использован. "
                    "Нажмите на странице «Показать код» и пришлите новый.")
        return ("Готово, чат и страница теперь ведут один дневник. Пишите про день - "
                "записи будут видны и здесь, и на странице.")

    @staticmethod
    def _as_text(messages: list[dict]) -> str:
        """
        Из сообщений ленты собираем обычный текст. Страница показывает их
        пузырями, в мессенджере пузырей нет - только строки.
        """
        blocks = []
        for message in messages:
            if message["kind"] == "me":
                continue        # своё сообщение человек и так видит
            block = message["text"]
            if message.get("note"):
                block += "\n" + message["note"]
            blocks.append(block)
        return "\n\n".join(blocks)

    def summary(self, chat_id: int) -> str:
        diary = self.store.diary_for_chat(chat_id)
        days = len(diary.timeline)
        if days < 7:
            return (f"В дневнике {days} дней. Для первых выводов нужно недели три, "
                    "чтобы набрались дни и с привычкой, и без неё.")
        links = [l for l in diary.links() if l.strength != "наблюдение"]
        if not links:
            return (f"Дней уже {days}, но ничего надёжного пока не видно. "
                    "Это тоже результат: значит, случайных совпадений вам не подсовываю.")
        lines = [f"За {days} дней нашлось:"]
        for link in links:
            lines.append(f"\n{say(link)}")
        return "\n".join(lines)

    # ── основной цикл ─────────────────────────────────────────────────────
    def process(self, update: dict) -> None:
        """Один апдейт: разобрать и ответить. Ошибка тут не должна ронять цикл."""
        message = update.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return          # посты в каналах и прочее без чата нам не адресованы
        try:
            answer = (self.handle_voice(message)
                      if message.get("voice") or message.get("audio")
                      else self.handle(message))
        except Exception as exc:
            print(f"Ошибка на сообщении: {self._hide(str(exc))}")
            answer = "Не получилось разобрать это сообщение. Попробуйте ещё раз."
        if answer:
            self.send(chat_id, answer)

    def run(self, stop: threading.Event | None = None,
            on_error=None) -> None:
        """
        Опрос Telegram. Останавливается через stop - это нужно, когда бота
        запустили со страницы и оттуда же выключают. on_error получает текст
        последней беды, чтобы её было видно человеку, а не только в консоли.
        """
        offset = 0
        failures = 0
        while stop is None or not stop.is_set():
            try:
                updates = self._call("getUpdates", offset=offset, timeout=self.timeout)
                failures = 0
                if on_error:
                    on_error("")
            except KeyboardInterrupt:
                print("\nОстановлено")
                return
            except Exception as exc:
                # Связи нет или Телеграм отказал: ждём тем дольше, чем дольше
                # не везёт, чтобы не колотиться в закрытую дверь.
                failures += 1
                text = self._hide(str(exc))
                print(f"Ошибка: {text}")
                if on_error:
                    on_error(text)
                self._pause(min(60, 3 * failures), stop)
                continue

            for update in updates.get("result", []):
                # Сдвигаем offset до обработки: иначе сообщение, на котором мы
                # спотыкаемся, придёт снова и цикл встанет на нём насовсем.
                offset = update["update_id"] + 1
                try:
                    self.process(update)
                except KeyboardInterrupt:
                    print("\nОстановлено")
                    return
                except Exception as exc:
                    # Не смогли даже ответить - остальные сообщения из пачки
                    # это не должно съедать.
                    print(f"Ошибка ответа: {self._hide(str(exc))}")

    @staticmethod
    def _pause(seconds: float, stop: threading.Event | None) -> None:
        if stop is None:
            time.sleep(seconds)
        else:
            stop.wait(seconds)


def main() -> None:
    bot = TelegramBot(store=DiaryStore(), parser=dialog.default_parser())
    print("Бот запущен. Напишите ему в Telegram. Остановить: Ctrl+C")
    bot.run()


if __name__ == "__main__":
    main()
