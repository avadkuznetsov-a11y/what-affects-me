"""
Мира в Telegram: основной способ вести дневник.

Человек пишет или наговаривает пару фраз, бот разбирает их, показывает, что
понял, и говорит, что об этих привычках уже известно. Раз в неделю можно
попросить разбор целиком.

Записи лежат в общем дневнике (wam.diary): если открыта страница Миры и человек
прислал боту код привязки, чат и страница ведут один дневник на двоих.

Режим server (MIRA_MODE=server) - бот сервиса для многих людей. Кода привязки
там нет: человек пишет /start, бот заводит его по номеру в Telegram и
присылает одноразовую ссылку на страницу. Пока человек не дал согласие на
обработку данных о здоровье, бот ничего не записывает. Этот режим включается
тем, что боту передан учёт людей (`accounts`); запускает его web/server.py.

Запуск:
    export TELEGRAM_TOKEN=...          # токен от @BotFather
    export SALUTE_SPEECH_TOKEN=...     # необязательно, для голосовых
    python3 -m bot.telegram

Сторонних библиотек нет: только стандартная библиотека, чтобы запускалось
где угодно без установки зависимостей.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from wam import dialog
from wam.accounts import user_key
from wam.diary import DiaryStore, looks_like_code
from wam import limits
from wam.limits import Limiter
from wam.llm import ssl_context
from wam.storage import DIARY_FILE
from wam.habits import wrote_that_day
from wam.phrases import say
from wam.voice import SaluteSpeech, transcribe_voice

from .reminders import DEFAULT_TIME, TEXT as REMINDER, Schedule

API = "https://api.telegram.org"
TIMEOUT = 30
MAX_FILE = 20 * 1024 * 1024      # больше Telegram и не отдаёт, а память не резиновая

# Токен от @BotFather выглядит всегда одинаково: номер бота, двоеточие, ключ.
# Проверяем форму до обращения к сети, чтобы не гонять запрос впустую.
TOKEN_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")

# Первое сообщение человек читает один раз и по диагонали. Раньше здесь была
# простыня в семь абзацев - половину её никто не дочитывал. Оставили суть:
# что делать прямо сейчас и один пример. Остальное - в /помощь.
HELLO = (
    "Привет, я Мира. Расскажу, что на вас влияет - по вашим же записям.\n\n"
    "Напишите или наговорите, как прошёл день. Например:\n"
    "«пил кофе часов в пять, спал часов пять, с утра тревога».\n\n"
    "Что ещё умею - /помощь"
)

HELP = (
    "Пишите про день обычными словами или голосовым - я разберу сам.\n\n"
    "Когда наберётся достаточно дней, скажу, что на вас влияет: не советом из "
    "интернета, а по вашим записям.\n\n"
    "Если открыта страница Миры, пришлите код привязки с неё - и чат со "
    "страницей будут вести один дневник.\n\n"
    f"Вечером, около {DEFAULT_TIME}, напомню записать день, если вы сегодня "
    "ничего не написали.\n\n"
    "/итоги - что уже известно\n"
    "/напоминание 20:30 - во сколько напоминать, /напоминание выкл\n"
    "/помощь - это сообщение"
)

# Команды, которые Telegram показывает подсказкой у поля ввода. Без этого
# человек про них не знает: обработчики есть, а меню пустое.
COMMANDS = [
    {"command": "итоги", "description": "что уже известно"},
    {"command": "напоминание", "description": "во сколько напоминать"},
    {"command": "помощь", "description": "что я умею"},
]

# Режим server: бот сервиса, у каждого свой дневник. Команды подписаны
# латиницей: кириллические Telegram в меню не принимает.
SERVICE_COMMANDS = [
    {"command": "login", "description": "ссылка для входа на страницу"},
    {"command": "summary", "description": "что уже известно"},
    {"command": "reminder", "description": "во сколько напоминать"},
    {"command": "export", "description": "как скачать свои записи"},
    {"command": "delete", "description": "удалить все мои данные"},
    {"command": "help", "description": "что я умею"},
]

SERVICE_HELLO = (
    "Привет, я Мира. Расскажу, что на вас влияет - по вашим же записям.\n\n"
    "Ваш дневник на странице: {link}\n"
    "Ссылка действует десять минут и открывается один раз. Войти снова - /login."
)

SERVICE_HELP = (
    "Пишите про день обычными словами или голосовым - я разберу сам.\n\n"
    "Когда наберётся достаточно дней, скажу, что на вас влияет: не советом из "
    "интернета, а по вашим записям.\n\n"
    f"Вечером, около {DEFAULT_TIME}, напомню записать день, если вы сегодня "
    "ничего не написали.\n\n"
    "/login - ссылка для входа на страницу\n"
    "/summary - что уже известно\n"
    "/reminder 20:30 - во сколько напоминать, /reminder выкл\n"
    "/export - как скачать свои записи\n"
    "/delete - удалить все мои данные\n"
    "/help - это сообщение"
)

CONSENT_ASK = (
    "Прежде чем я начну записывать, нужно ваше согласие на обработку данных о "
    "здоровье: того, что вы рассказываете про сон, самочувствие и привычки. "
    "Что это значит и как всё удалить: {privacy}\n\n"
    "Согласиться можно кнопкой ниже или на странице дневника."
)

CONSENT_BUTTON = {"inline_keyboard": [[{"text": "Согласен", "callback_data": "consent"}]]}

# Подтверждение удаления: слово, которое случайно не напишешь, и срок, после
# которого просьба об удалении забывается.
ERASE_WORD = "УДАЛИТЬ"
ERASE_WAIT = 5 * 60

# Сколько сообщений в минуту принимаем от одного чата. Живой человек столько
# не пишет; больше - это скрипт, и каждое его сообщение стоит разбора моделью.
CHAT_LIMIT = 30

# Как выключить и включить напоминания словом, а не временем.
REMINDER_OFF = ("выкл", "выключить", "отключить", "не надо", "off", "нет")
REMINDER_ON = ("вкл", "включить", "on", "да")


class TelegramError(RuntimeError):
    """Телеграм ответил отказом или не ответил совсем."""


class Reply(str):
    """
    Ответ с настройками отправки: кнопками или выключенным превью ссылки.
    Это та же строка, поэтому всё, что сравнивает ответы бота с текстом,
    работает как раньше, а `process` достаёт настройки из `options`.
    """

    options: dict

    def __new__(cls, text: str, **options):
        reply = super().__new__(cls, text)
        reply.options = options
        return reply


def trace(exc: BaseException) -> str:
    """
    Что написать в журнал про ошибку. Текст исключения в журнал не идёт: в нём
    бывает кусок того, что человек рассказал про своё здоровье (разбор речи
    падает на конкретной фразе). Тип и короткий отпечаток текста дают
    отличить одну беду от другой и не раскрывают ничего.
    """
    digest = hashlib.sha256(str(exc).encode("utf-8", "replace")).hexdigest()[:8]
    return f"{type(exc).__name__} #{digest}"


def token_is_shaped_right(token: str) -> bool:
    return bool(TOKEN_SHAPE.fullmatch((token or "").strip()))


class TelegramBot:
    def __init__(self, token: str | None = None, store: DiaryStore | None = None,
                 parser: dialog.Parser | None = None, timeout: int = TIMEOUT,
                 accounts=None, public_url: str = "") -> None:
        """
        accounts и public_url - режим server: учёт людей (wam.accounts.Accounts)
        и адрес страницы, на который бот шлёт ссылки входа. Без них бот
        работает как раньше - один дневник и код привязки.
        """
        self.token = (token or os.environ.get("TELEGRAM_TOKEN", "")).strip()
        if not self.token:
            raise RuntimeError("не задан TELEGRAM_TOKEN")
        self.store = store or DiaryStore()
        self.parser = parser
        self.timeout = timeout
        self.speech = SaluteSpeech() if os.environ.get("SALUTE_SPEECH_TOKEN") else None
        self.accounts = accounts
        self.public_url = public_url.rstrip("/")
        if accounts is not None:
            # Расписание лежит в базе, а потолка нет: в нём только люди,
            # давшие согласие, и их ровно столько, сколько пользователей.
            self.reminders = Schedule(storage=accounts.storage, max_chats=None)
        else:
            self.reminders = Schedule()
        self.limit = Limiter(CHAT_LIMIT, 60)
        self._erasing: dict[int, float] = {}     # чат -> когда попросил удалить всё

    @property
    def service(self) -> bool:
        return self.accounts is not None

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
                                        timeout=self.timeout + 5,
                                        context=ssl_context()) as r:
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
        name = self._call("getMe")["result"].get("username", "")
        self.publish_commands(SERVICE_COMMANDS if self.service else COMMANDS)
        return name

    def publish_commands(self, commands: list[dict] = COMMANDS) -> None:
        """
        Показать команды в меню у поля ввода. Обработчики без этого работают,
        но человек про команды не знает: их негде увидеть.

        Отказ не важен: команды - удобство, а не работа бота.
        """
        try:
            self._call("setMyCommands", commands=json.dumps(commands, ensure_ascii=False))
        except Exception:
            pass

    def send(self, chat_id: int, text: str, **options) -> None:
        """Отправить сообщение. Кнопки и прочие сложные поля Telegram ждёт в JSON."""
        extra = {name: json.dumps(value, ensure_ascii=False)
                 if isinstance(value, (dict, list)) else value
                 for name, value in options.items()}
        self._call("sendMessage", chat_id=chat_id, text=text, **extra)

    def download(self, file_id: str) -> bytes:
        info = self._call("getFile", file_id=file_id)
        path = info["result"]["file_path"]
        with urllib.request.urlopen(f"{API}/file/bot{self.token}/{path}",
                                    timeout=self.timeout, context=ssl_context()) as r:
            return r.read(MAX_FILE)

    # ── обработка сообщений ───────────────────────────────────────────────
    def handle(self, message: dict) -> str:
        """Разобрать одно сообщение и вернуть ответ. Сети тут нет - удобно тестировать."""
        chat_id = message.get("chat", {}).get("id")
        if chat_id is None:
            return ""       # без чата отвечать некому, и дневник заводить не на кого
        if self.service:
            return self._serve(message)
        text = (message.get("text") or "").strip()
        # Напоминаем только тем, кто сам написал боту: рассылать незнакомым
        # некому и незачем.
        self.reminders.remember(chat_id)

        if text.startswith("/start"):
            return HELLO
        if text.startswith("/помощь") or text.startswith("/help"):
            return HELP
        if text.startswith("/итоги") or text.startswith("/summary"):
            return self.summary(chat_id)
        if text.startswith("/напоминание") or text.startswith("/reminder"):
            return self.set_reminder(chat_id, text.split(maxsplit=1)[1:])
        if not text:
            return "Пока понимаю только текст и голосовые. Напишите пару фраз о дне."

        if looks_like_code(text):
            return self._bind(text, chat_id)

        diary = self.store.diary_for_chat(chat_id)
        # Связи считаем один раз за сообщение: раньше они считались трижды,
        # и на дневнике в месяц бот замолкал на десяток секунд.
        messages = dialog.step(diary, text, links=diary.links(),
                               hints=diary.hints(),
                               parser=self.parser, origin="chat")
        return self._as_text(messages)

    def handle_voice(self, message: dict) -> str:
        """Голосовое: скачиваем, распознаём, дальше как обычный текст."""
        if self.service:
            # Голос уходит на распознавание в чужой сервис - это уже обработка.
            # Без согласия не скачиваем и не распознаём ничего.
            refusal = self._gate(message)
            if refusal is not None:
                return refusal
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

    # ── режим server ──────────────────────────────────────────────────────
    def _serve(self, message: dict) -> str:
        """
        Сообщение в боте сервиса. Человек - это номер отправителя в Telegram,
        дневник у каждого свой (`u:<номер>`), кодов привязки нет.
        """
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""

        if not self._private(message):
            return ""

        if command in ("/start", "/login"):
            return self._welcome(message, first=command == "/start")
        if command in ("/help", "/помощь"):
            return SERVICE_HELP

        # Удалить свои данные можно и без согласия: оно нужно, чтобы записывать,
        # а не чтобы уйти.
        if self.accounts.user(chat_id) is None:
            return "Напишите /start - так я заведу ваш дневник."
        if command == "/delete":
            self._erasing[chat_id] = time.time()
            return ("Это удалит всё: записи, напоминания и вход на страницу. Вернуть "
                    f"будет нельзя. Чтобы подтвердить, напишите одно слово: {ERASE_WORD}.\n"
                    "Передумали - просто напишите что-нибудь другое.")
        asked = self._erasing.pop(chat_id, None)
        if asked is not None and time.time() - asked <= ERASE_WAIT and text == ERASE_WORD:
            self.erase(chat_id)
            return ("Готово: ваши записи, напоминания и вход на страницу удалены. "
                    "Из резервных копий они уйдут в течение 14 дней. Чтобы начать "
                    "заново, напишите /start.")

        refusal = self._gate(message)
        if refusal is not None:
            return refusal
        if command == "/export":
            return ("Скачать записи можно на странице, в блоке «Мои данные»: там дневник "
                    "текстом и таблицей для Excel. Ссылка для входа - /login.")
        if command in ("/summary", "/итоги"):
            return self.summary(chat_id)
        if command in ("/reminder", "/напоминание"):
            return self.set_reminder(chat_id, text.split(maxsplit=1)[1:])
        if not text:
            return "Пока понимаю только текст и голосовые. Напишите пару фраз о дне."
        # Потолок разбора общий со страницей: человек один, бюджет на модель один
        refusal = limits.may_parse(chat_id)
        if refusal:
            return refusal

        diary = self._diary_of(chat_id)
        messages = dialog.step(diary, text, links=diary.links(),
                               hints=diary.hints(),
                               parser=self.parser, origin="chat")
        return self._as_text(messages)

    @staticmethod
    def _private(message: dict) -> bool:
        """
        Только личная переписка. В группе номер чата - не номер человека, и
        записи одного легли бы в дневник, который видят все участники.
        """
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return chat.get("type", "private") == "private" and sender.get("id") == chat.get("id")

    def _welcome(self, message: dict, first: bool) -> Reply:
        """Завести человека, привязать чат и прислать одноразовую ссылку входа."""
        user_id = message["chat"]["id"]
        self.accounts.enroll(user_id, (message.get("from") or {}).get("username", ""))
        self.store.attach(user_id, user_key(user_id))
        link = f"{self.public_url}/login#t={self.accounts.issue_login(user_id)}"
        if first:
            text = SERVICE_HELLO.format(link=link)
        else:
            text = (f"Ссылка для входа: {link}\n"
                    "Действует десять минут и открывается один раз.")
        # Превью выключаем: сервис превью сам ходит по ссылке, и токен,
        # который мы так берегли, уехал бы на чужой сервер.
        options = {"link_preview_options": {"is_disabled": True}}
        if not self.accounts.has_consent(user_id):
            text += "\n\n" + CONSENT_ASK.format(privacy=f"{self.public_url}/privacy")
            options["reply_markup"] = CONSENT_BUTTON
        else:
            self.reminders.remember(user_id)
            if first:
                text += "\n\nНапишите или наговорите, как прошёл день."
        return Reply(text, **options)

    def _gate(self, message: dict) -> str | None:
        """
        Можно ли принимать от человека записи. None - можно; иначе ответ,
        почему нельзя. Пока нет согласия, ничего не пишем в дневник.
        """
        if not self._private(message):
            return ""
        user_id = message["chat"]["id"]
        if self.accounts.user(user_id) is None:
            return "Напишите /start - так я заведу ваш дневник."
        if not self.accounts.has_consent(user_id):
            return Reply("Сначала подтвердите согласие. "
                         + CONSENT_ASK.format(privacy=f"{self.public_url}/privacy"),
                         link_preview_options={"is_disabled": True},
                         reply_markup=CONSENT_BUTTON)
        # Напоминаем только тем, кто согласился и сам пишет боту
        self.reminders.remember(user_id)
        return None

    def consent(self, query: dict) -> None:
        """Нажата кнопка «Согласен» под сообщением бота."""
        user_id = (query.get("from") or {}).get("id")
        try:
            self._call("answerCallbackQuery", callback_query_id=query.get("id", ""))
        except TelegramError:
            pass        # часики на кнопке погаснут сами, согласие важнее
        if query.get("data") != "consent" or user_id is None:
            return
        if self.accounts.user(user_id) is None:
            self.send(user_id, "Напишите /start - так я заведу ваш дневник.")
            return
        self.accounts.agree(user_id)
        self.reminders.remember(user_id)
        self.send(user_id, "Спасибо. Теперь пишите или наговаривайте, как прошёл день.")

    def erase(self, user_id: int) -> None:
        """
        Удалить человека целиком: дневник, привязку чата, напоминания, сессии
        и учётную запись. То же самое делает кнопка «Удалить всё» на странице.
        """
        self.accounts.erase(user_id, self.store)
        self.reminders.forget(user_id)
        self._erasing.pop(user_id, None)

    def _diary_of(self, chat_id: int):
        """
        Дневник этого чата. В режиме server - всегда дневник самого человека:
        гостевых дневников tg:* там не бывает.
        """
        if self.service:
            return self.store.get(user_key(chat_id))
        return self.store.diary_for_chat(chat_id)

    def _bind(self, code: str, chat_id: int) -> str:
        if self.store.bind(code, chat_id) is None:
            if self.store.code_paused(chat_id):
                # Код подбирают. Отвечать нельзя: любой ответ говорит подбирающему,
                # что попытка засчитана, - молчим, пока пауза не выйдет.
                return ""
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

    # ── напоминания ───────────────────────────────────────────────────────
    def set_reminder(self, chat_id: int, argument: list[str]) -> str:
        """Ответ на /напоминание: показать время, поменять его или выключить."""
        said = (argument[0] if argument else "").strip().lower()

        if not said:
            at = self.reminders.time_of(chat_id)
            if not at:
                return ("Напоминания выключены. Включить обратно: "
                        "/напоминание вкл или /напоминание 20:30.")
            return (f"Напоминаю в {at}, и только если вы за день ничего не написали. "
                    "Поменять: /напоминание 20:30. Выключить: /напоминание выкл.")

        if said in REMINDER_OFF:
            self.reminders.turn_off(chat_id)
            return "Больше не напоминаю. Передумаете - /напоминание вкл."
        if said in REMINDER_ON:
            return f"Хорошо, напомню в {self.reminders.turn_on(chat_id)}."

        try:
            at = self.reminders.set_time(chat_id, said)
        except ValueError:
            return "Не понял время. Напишите так: /напоминание 20:30."
        return f"Буду напоминать в {at}, если за день от вас ничего не будет."

    def _wrote_today(self, chat_id: int, today: date) -> bool:
        """
        Писал ли человек сегодня хоть куда-нибудь. Смотрим дневник, а не чат:
        запись могла прийти и со страницы, а напоминание после неё выглядит
        так, будто мы его не услышали.
        """
        diary = self._diary_of(chat_id)
        with diary.lock:
            return any(record.day == today and wrote_that_day(record)
                       for record in diary.timeline.days)

    def remind(self, now: datetime | None = None) -> list[int]:
        """
        Разослать напоминания, кому пора. Возвращает чаты, которым написали.

        Зовётся из цикла опроса между запросами к Telegram, поэтому падать тут
        нельзя: не отправилось одному - остальные не при чём.
        """
        now = now or datetime.now()
        sent: list[int] = []
        for chat_id in self.reminders.chats():
            if not self.reminders.due(chat_id, now):
                continue
            if self._wrote_today(chat_id, now.date()):
                # Человек уже рассказал про день - напоминание было бы
                # невежливым. День всё равно закрываем, чтобы не проверять его
                # заново каждые полминуты.
                self.reminders.mark_sent(chat_id, now.date())
                continue
            try:
                self.send(chat_id, REMINDER)
            except Exception as exc:
                print(f"Напоминание не ушло: {self._trace(exc)}")
                continue
            self.reminders.mark_sent(chat_id, now.date())
            sent.append(chat_id)
        return sent

    def summary(self, chat_id: int) -> str:
        diary = self._diary_of(chat_id)
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
        query = update.get("callback_query")
        if query and self.service:
            if self.limit.allow((query.get("from") or {}).get("id")):
                self.consent(query)
            return
        message = update.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return          # посты в каналах и прочее без чата нам не адресованы
        if self.service and not self.limit.allow(chat_id):
            # Сверх потолка молчим: ответ «слишком часто» стоит столько же,
            # сколько обычный, а скрипту он ничего не объяснит.
            return
        try:
            answer = (self.handle_voice(message)
                      if message.get("voice") or message.get("audio")
                      else self.handle(message))
        except Exception as exc:
            print(f"Ошибка на сообщении: {trace(exc)}")
            answer = "Не получилось разобрать это сообщение. Попробуйте ещё раз."
        if answer:
            options = getattr(answer, "options", None)
            if options:
                self.send(chat_id, str(answer), **options)
            else:
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
            # Отдельного потока для напоминаний не заводим: getUpdates всё
            # равно возвращается не реже, чем раз в timeout секунд, и этого
            # для расписания с точностью до минуты более чем достаточно.
            try:
                self.remind()
            except Exception as exc:
                print(f"Напоминания: {self._trace(exc)}")

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
                    print(f"Ошибка ответа: {self._trace(exc)}")

    def _trace(self, exc: BaseException) -> str:
        """
        Отказ Telegram пишем как есть, без токена: в нём только метод и причина
        от самого Telegram, а без причины не понять, что сломалось. Любую
        другую ошибку - только типом и отпечатком, см. trace().
        """
        if isinstance(exc, TelegramError):
            return self._hide(str(exc))
        return trace(exc)

    @staticmethod
    def _pause(seconds: float, stop: threading.Event | None) -> None:
        if stop is None:
            time.sleep(seconds)
        else:
            stop.wait(seconds)


def main() -> None:
    # Тот же файл дневника, что и у страницы: бот и страница - две двери в одни
    # и те же записи. Запускать бота отдельно от страницы имеет смысл, только
    # если страница не работает: два процесса на один файл будут затирать друг
    # другу дни, каждый своей памятью.
    # MIRA_DB - тот же путь к базе, что у страницы: иначе бот и страница,
    # запущенные с одной настройкой, разошлись бы по двум файлам.
    bot = TelegramBot(store=DiaryStore(os.environ.get("MIRA_DB") or DIARY_FILE),
                      parser=dialog.default_parser())
    print("Бот запущен. Напишите ему в Telegram. Остановить: Ctrl+C")
    bot.run()


if __name__ == "__main__":
    main()
