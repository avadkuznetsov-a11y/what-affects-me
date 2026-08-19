"""
Мира в Telegram: основной способ вести дневник.

Человек пишет или наговаривает пару фраз, бот разбирает их, показывает, что
понял, и говорит, что об этих привычках уже известно. Раз в неделю можно
попросить разбор целиком.

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
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from wam.derive import DEVICE_SOURCES, derive_factors
from wam.extract import RuleExtractor
from wam.insights import find_links
from wam.phrases import full, say
from wam.questions import next_question
from wam.schema import Timeline
from wam.voice import SaluteSpeech, transcribe_voice

API = "https://api.telegram.org"
TIMEOUT = 30
MAX_FILE = 20 * 1024 * 1024      # больше Telegram и не отдаёт, а память не резиновая


class TelegramError(RuntimeError):
    """Телеграм ответил отказом или не ответил совсем."""

HELP = (
    "Привет, я Мира - дневник, который ищет причины.\n\n"
    "Расскажите, как прошёл день - обычными словами или голосовым.\n\n"
    "Например: «пил кофе часов в пять, спал часов пять, с утра тревога».\n\n"
    "Я запомню, что было и как вы себя чувствовали. Когда наберётся достаточно "
    "дней, скажу, что на вас влияет.\n\n"
    "/итоги - что уже известно\n"
    "/помощь - это сообщение"
)


class TelegramBot:
    def __init__(self, token: str | None = None, storage: "Storage | None" = None) -> None:
        self.token = token or os.environ.get("TELEGRAM_TOKEN", "")
        if not self.token:
            raise RuntimeError("не задан TELEGRAM_TOKEN")
        self.storage = storage or Storage()
        self.extractor = RuleExtractor()
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
                                        timeout=TIMEOUT + 5) as r:
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

    def send(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", chat_id=chat_id, text=text)

    def download(self, file_id: str) -> bytes:
        info = self._call("getFile", file_id=file_id)
        path = info["result"]["file_path"]
        with urllib.request.urlopen(f"{API}/file/bot{self.token}/{path}", timeout=TIMEOUT) as r:
            return r.read(MAX_FILE)

    # ── обработка сообщений ───────────────────────────────────────────────
    def handle(self, message: dict) -> str:
        """Разобрать одно сообщение и вернуть ответ. Сети тут нет — удобно тестировать."""
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

        record = self.extractor.extract(text, date.today())
        if not record.facts:
            return next_question(record)

        self.storage.add(chat_id, record)
        return self._reply(chat_id, record)

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

    def _reply(self, chat_id: int, record) -> str:
        lines = ["Записал:"]
        for fact in record.facts:
            mark = "с кольца" if fact.source in DEVICE_SOURCES else ""
            if fact.kind == "factor":
                lines.append(f"  {fact.name}: {'было' if fact.value else 'не было'} {mark}".rstrip())
            else:
                lines.append(f"  {fact.name}: {fact.value} из 10 {mark}".rstrip())

        question = next_question(record, find_links(derive_factors(self.storage.timeline(chat_id))))
        if question:
            lines.append(f"\n{question}")

        known = self._known_for(chat_id, record)
        if known:
            lines.append("\nЧто про это уже известно:")
            lines.extend(f"  {line}" for line in known)
        else:
            days = len(self.storage.timeline(chat_id))
            lines.append(f"\nВсего дней в дневнике: {days}. Выводы появятся, когда наберётся "
                         "хотя бы семь дней с привычкой и семь без неё.")
        return "\n".join(lines)

    def _known_for(self, chat_id: int, record) -> list[str]:
        timeline = derive_factors(self.storage.timeline(chat_id))
        mentioned = {f.name for f in record.facts if f.kind == "factor" and f.value > 0}
        out = []
        for link in find_links(timeline):
            if link.factor not in mentioned or link.strength == "наблюдение":
                continue
            out.append(full(link))
        return out

    def summary(self, chat_id: int) -> str:
        timeline = derive_factors(self.storage.timeline(chat_id))
        if len(timeline) < 7:
            return (f"В дневнике {len(timeline)} дней. Для первых выводов нужно недели три, "
                    "чтобы набрались дни и с привычкой, и без неё.")
        links = [l for l in find_links(timeline) if l.strength != "наблюдение"]
        if not links:
            return (f"Дней уже {len(timeline)}, но ничего надёжного пока не видно. "
                    "Это тоже результат: значит, случайных совпадений вам не подсовываю.")
        lines = [f"За {len(timeline)} дней нашлось:"]
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

    def run(self) -> None:
        print("Бот запущен. Напишите ему в Telegram. Остановить: Ctrl+C")
        offset = 0
        failures = 0
        while True:
            try:
                updates = self._call("getUpdates", offset=offset, timeout=TIMEOUT)
                failures = 0
            except KeyboardInterrupt:
                print("\nОстановлено")
                return
            except Exception as exc:
                # Связи нет или Телеграм отказал: ждём тем дольше, чем дольше
                # не везёт, чтобы не колотиться в закрытую дверь.
                failures += 1
                print(f"Ошибка: {self._hide(str(exc))}")
                time.sleep(min(60, 3 * failures))
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


class Storage:
    """
    Дневники по чатам. Живут в памяти процесса: перезапустили бота - записи
    начинаются заново. Для прототипа этого достаточно, база появится вместе
    с личным кабинетом.
    """

    def __init__(self) -> None:
        self._data: dict[int, Timeline] = {}

    def timeline(self, chat_id: int) -> Timeline:
        return self._data.setdefault(chat_id, Timeline())

    def add(self, chat_id: int, record) -> None:
        timeline = self.timeline(chat_id)
        for existing in timeline.days:
            if existing.day == record.day:      # запись за тот же день дополняет прежнюю
                existing.facts.extend(record.facts)
                return
        timeline.add(record)


def main() -> None:
    TelegramBot().run()


if __name__ == "__main__":
    main()
