"""
Локальная страница-диалог: так же, как продукт будет работать в мессенджере.

Запуск:  python3 -m web.server
Откроется на http://127.0.0.1:8765 - ни ключей, ни интернета не нужно.

Человек пишет про свой день, программа отвечает: что записала, чего не хватает
и что уже известно. Когда данных достаточно, показывает выводы. Сервер слушает
только 127.0.0.1 и наружу ничего не отдаёт.

Это режим по умолчанию, MIRA_MODE=local: один человек на своей машине, один
дневник, без входа. MIRA_MODE=server - сервис для многих людей: у каждого свой
дневник, вход по личному коду или по ссылке от бота, согласие, удаление
данных. Telegram в режиме server необязателен: без TELEGRAM_TOKEN сервис
работает только через сайт. Чем режимы
отличаются, расписано в docs/PLAN_SERVER.md и в README.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import signal
import sys
import threading
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.telegram import TelegramBot, token_is_shaped_right, trace
from demo.generate import build
from wam import dialog, export, food, weather
from wam import accounts, limits
from wam.accounts import (CONSENT_VERSION, COOKIE, SESSION_LIFETIME, Accounts,
                          csrf_for, user_key)
from wam.derive import derive_factors
from wam.diary import DiaryStore
from wam import storage
from wam.experiments import Experiment, can_try, evaluate
from wam.insights import MIN_DAYS_PER_GROUP, OBSERVATION_MIN_DAYS, find_links
from wam.llm import available_engine
from wam.phrases import (basis, compared, days_count, guess, headline, next_step, period,
                         say)
from wam.questions import day_name
from wam.today import HINT_EFFECT, HINT_P

# Режим читаем один раз при запуске. Неизвестное слово - ошибка сразу: молча
# поднять сервис для многих людей в режиме «один человек без входа» значит
# открыть все дневники всем.
MODE = os.environ.get("MIRA_MODE", "").strip().lower() or "local"
if MODE not in ("local", "server"):
    raise SystemExit(f"MIRA_MODE: ожидается local или server, а не «{MODE}»")

# Адрес, под которым сервис виден людям. Только по нему сверяем Host и Origin:
# в режиме server страница приходит не с 127.0.0.1, а через nginx-proxy.
PUBLIC_HOST = os.environ.get("MIRA_PUBLIC_HOST", "").strip().lower()

# Путь к базе. На сервере база лежит в смонтированном каталоге data, у
# человека на машине - рядом с программой, как и раньше. Путь по умолчанию
# берём из модуля, а не копией: тесты подменяют его на временный.
DB_FILE = Path(os.environ["MIRA_DB"]) if os.environ.get("MIRA_DB") else storage.DIARY_FILE

# Готовые ответы разговора ссылаются на кнопки страницы, а они у режимов разные:
# в server нет ни файла дневника у человека, ни раздела «Telegram».
dialog.SERVICE = MODE == "server"

# В контейнере слушаем все адреса: снаружи до порта дотягивается только
# nginx-proxy из общей сети docker, сам порт наружу не опубликован.
HOST = "0.0.0.0" if MODE == "server" else "127.0.0.1"
PORT = 8765
DEFAULT_DAYS = 120
WEB_KEY = "web"          # ключ дневника страницы в режиме local

# Сроки дневника, которые можно выбрать на странице. Список один на программу:
# страница строит из него выпадающий список, а сервер теми же словами называет
# срок в выводах. Пока слова были в двух местах, выходило «21 дней» и «6 месяца».
PERIODS: tuple[tuple[int, str], ...] = (
    (21, "3 недели"),
    (45, "1,5 месяца"),
    (90, "3 месяца"),
    (120, "4 месяца"),
    (180, "полгода"),
)

# Кольцо и КБЖУ. В режиме local это стенд: ползунки кольца стоят на
# придуманных значениях, чтобы показать, как показания приборов ложатся в
# дневник. В режиме server настоящего кольца ни у кого нет, и придуманные
# цифры записывались в настоящий дневник человека («качество сна 4.1 из 10
# (с кольца)»). Решение владельца 11.11.2026: в server по умолчанию оба
# источника выключены - сервер их показания не принимает, страница пишет
# «пока не подключено». Включать вместе с настоящим подключением прибора или
# с ручным вводом, подписанным «введено вручную», а не «с кольца».
DEVICES = MODE != "server"

# Речь разбирает модель, если для неё есть ключ в окружении. Ключей в
# репозитории нет: у того, кто скачает код, будет работать разбор по правилам,
# пока он не подставит свой ключ.
#
# На сервере разрешён только YandexGPT: туда уходят записи о здоровье многих
# людей, и сервис выбирает владелец, а не случайный ключ в окружении. Нет
# ключа - разбор по правилам.
ENGINE_NAME, _COMPLETE = available_engine(only=("YandexGPT",) if MODE == "server" else None)
PARSER = dialog.Parser(ENGINE_NAME, _COMPLETE)

# Дневник один на процесс: то, что человек написал боту в Telegram, видно на
# странице, и наоборот. Лежит он в файле рядом с программой и переживает
# перезапуск - человек ведёт дневник неделями.
if MODE == "server":
    # Каталог data на сервере может быть ещё пустым томом: заводим его до
    # хранилища, иначе база не откроется и сервис не поднимется.
    DB_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
STORE = DiaryStore(DB_FILE)

# Учёт людей: нужен только в режиме server, но таблицы лежат в том же файле и
# в режиме local просто пустуют. Без файла (база недоступна) учёта нет, и
# режим server на таком не запустится - см. main().
ACCOUNTS = Accounts(STORE.storage) if STORE.storage is not None else None

# Имя бота сервиса: страница называет его тем, кто ещё не вошёл. Узнаём при
# запуске у Telegram (getMe), в режиме local не нужно.
BOT_NAME = ""

# Бот сервиса в режиме server. Через него же идёт удаление человека со
# страницы: у бота в памяти расписание напоминаний, и его надо почистить тоже.
# None - TELEGRAM_TOKEN не задан: сервис работает только через сайт, а всё
# про Telegram на странице спрятано.
SERVICE_BOT: TelegramBot | None = None

MAX_TEXT = 4000          # длиннее человек за раз не пишет, а разбирать дорого
MAX_BODY = 64 * 1024
MAX_CITY = 80            # самое длинное название города в России короче вдвое

# Показания прибора: свой предел на каждое. Прежний общий потолок в миллион
# спасал только от падения - sleep_score 999999 разбор честно пересчитывал в
# «качество сна 99999,9 из 10», и в ленте появлялась бессмыслица. Сон, стресс,
# сатурация - шкалы до сотни; пульс покоя выше 220 не бывает ни у кого живого;
# шаги считаются тысячами, рекорд суточной ходьбы - около 100 тысяч.
MAX_READING = {
    "sleep_score": 100, "sleepAnalysis": 100,
    "stress_level": 100, "energy": 100,
    "spo2": 100, "oxygenSaturation": 100,
    "resting_hr": 220, "restingHeartRate": 220,
    "steps": 100_000, "stepCount": 100_000,
}
DEFAULT_READING = 100      # поле, которого мы не знаем, разбор всё равно не возьмёт

# Пределы для КБЖУ, введённого руками. Границы взяты с большим запасом от
# всего, что человек способен съесть за сутки: дело тут не в норме, а в том,
# чтобы опечатка на лишний ноль не стала фактором дня.
MAX_FOOD = {"калории": 20_000.0, "белки": 1000.0, "жиры": 1000.0, "углеводы": 2000.0}

# Выгрузка КБЖУ: строка на день - десятки байт, год строк укладывается в
# десяток килобайт. Больше общего предела на тело запроса всё равно не пройдёт.
MAX_CSV = MAX_BODY // 2
# Из выгрузки КБЖУ берём дни только за последние два года и до сегодня.
FOOD_WINDOW_DAYS = 2 * 366
# Потолок дней в одном дневнике. Два года каждый день с запасом: больше
# человек за пилот не наберёт, а держать в памяти дневник без конца нельзя.
MAX_DIARY_DAYS = 800


def period_name(days: int) -> str:
    """
    Как назвать срок словами. Для сроков из списка берём то же название, что
    человек видел в списке, для любого другого числа - обычную фразу по-русски.
    """
    for known, name in PERIODS:
        if known == days:
            return name
    return period(days)


def page() -> bytes:
    """
    Разметка страницы лежит рядом, в page.html: держать её строкой внутри
    модуля неудобно - редактор не подсказывает ни в html, ни в javascript.
    """
    html = (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
    # Сроки уезжают внутрь <script>, поэтому «</» в них закрыло бы тег и
    # остаток строки браузер прочёл бы как разметку. Сейчас там константы, но
    # страховка дешевле разбирательства, если список станет настраиваемым.
    periods = json.dumps([{"days": days, "name": name} for days, name in PERIODS],
                         ensure_ascii=False).replace("</", "<\\/")
    html = (html.replace("__ENGINE__", engine_note())
                .replace("__PERIODS__", periods)
                .replace("__DEFAULT_DAYS__", str(DEFAULT_DAYS))
                .replace("__BOT__", BOT_NAME)
                .replace("__DEVICES__", "1" if DEVICES else "")
                # Есть ли у сервиса бот: без него страница не зовёт в Telegram
                .replace("__TG__", "1" if SERVICE_BOT is not None else ""))
    return html.encode("utf-8")


def engine_note() -> str:
    """
    Кто разбирает речь - одной фразой внизу «Моих данных». Про Яндекс говорим
    и то, что запросы у него не остаются: заголовок x-data-logging-enabled в
    wam/llm.py. Про другие сервисы такого не обещаем - не проверяли.
    """
    if ENGINE_NAME == "YandexGPT":
        return "Речь разбирает YandexGPT, без сохранения у Яндекса."
    if ENGINE_NAME == "правила":
        return "Речь разбирают правила прямо в программе, без внешних сервисов."
    return f"Речь разбирает {html.escape(ENGINE_NAME)}."


# Шрифты страницы: отдаём только эти файлы и только по точному имени - путь
# из запроса к диску не приклеивается, выйти из каталога нечем.
FONTS_DIR = Path(__file__).parent / "fonts"
FONTS = {f"/fonts/{path.name}": path for path in FONTS_DIR.glob("*.woff2")}

# Знак Миры в боковой панели. Отдельный файл, а не картинка внутри html: страница
# собирается на каждый запрос, и двадцать килобайт base64 ехали бы каждый раз.
MARK = Path(__file__).parent / "mira-mark.png"


# ── состояние разговора ───────────────────────────────────────────────────

_CACHE: dict[int, tuple] = {}
_CACHE_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _cache_lock(days: int) -> threading.Lock:
    """
    Свой замок на каждый срок. Полгода считаются секунд семь, и общий замок на
    это время подвешивал все остальные запросы - даже те, у которых ответ уже
    посчитан. Замков не больше, чем сроков: число дней зажато между 14 и 365.
    """
    with _LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(days, threading.Lock())


def _diary(days: int = DEFAULT_DAYS):
    """Придуманный дневник и найденные в нём связи. Считаем один раз на срок."""
    ready = _CACHE.get(days)
    if ready is not None:
        return ready
    with _cache_lock(days):
        # пока ждали замок, соседний запрос мог всё посчитать
        if days not in _CACHE:
            timeline = derive_factors(build(days=days))
            _CACHE[days] = (timeline, find_links(timeline))
        return _CACHE[days]


class TelegramRunner:
    """
    Бот, запущенный со страницы.

    Токен живёт только здесь, в поле объекта в памяти процесса: ни в файле, ни
    в логе, ни в одном ответе сервера его нет. Перезапустили программу - токен
    вводится заново, и это правильно: чужой токен на диске никому не нужен.
    """

    LONG_POLL = 10      # со страницы бота выключают кнопкой, ждать 30 секунд незачем

    def __init__(self, store: DiaryStore, key: str = WEB_KEY) -> None:
        self.store = store
        self.key = key
        self._lock = threading.Lock()
        self._token = ""
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self.username = ""
        self.code = ""
        self.error = ""

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def connect(self, token: str) -> str:
        """
        Проверить токен и начать опрос. Возвращает имя бота. Форму токена
        проверяем до сети, чтобы не гонять запрос впустую.
        """
        token = (token or "").strip()
        if not token_is_shaped_right(token):
            raise ValueError("Это не похоже на токен от BotFather. "
                             "Он выглядит так: 1234567890:строка-из-букв-и-цифр.")
        if self.connected and token == self._token:
            # Второй поток опроса на том же токене заводить нельзя: Telegram
            # ответит 409 и сообщения начнут пропадать у обоих.
            return self.username

        bot = TelegramBot(token=token, store=self.store, parser=PARSER,
                          timeout=self.LONG_POLL)
        username = bot.username()       # сетевой вызов до всяких замков

        with self._lock:
            self._halt()
            self._token = token
            self.username = username
            self.error = ""
            self._issue_code()      # код даём только после getMe
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=bot.run, kwargs={"stop": self._stop, "on_error": self._note},
                daemon=True)
            self._thread.start()
        return username

    def disconnect(self) -> None:
        """
        Выключить бота. Заодно отвязываем чаты: пока этого не было, «Отключить»
        оставляло чужому чату доступ к дневнику - отозвать его было нечем.
        """
        with self._lock:
            self._halt()
            self.store.unlink(self.key)
            self._token = ""
            self.username = ""
            self.code = ""
            self.error = ""

    def unlink(self, chat_id: int | None = None) -> None:
        """
        Отвязать один чат или все сразу; бот при этом остаётся на связи.

        Новый код после отвязки не выдаём: человек нажал «Отвязать», чтобы
        закрыть доступ, а не чтобы открыть новое окно привязки. Понадобится -
        на странице есть кнопка «Показать новый код».
        """
        with self._lock:
            if chat_id is None:
                self.store.unlink(self.key)
            else:
                self.store.unlink_chat(chat_id, self.key)

    def new_code(self) -> str:
        """Новый код привязки. Без подключённого бота он бессмысленен."""
        with self._lock:
            if not self.connected:
                return ""
            return self._issue_code()

    def state(self) -> dict:
        """Что показать в панели. Токена тут нет и быть не может."""
        chats = self.store.linked_chats(self.key)
        # Свежесть кода знает только хранилище: код гаснет и по времени, и от
        # перебора из многих чатов. По своим часам выходило, что код ещё жив,
        # а бот на него уже не отзывался.
        live = bool(self.code) and self.store.code_is_live(self.code)
        return {
            "connected": self.connected,
            "username": self.username,
            "linked": bool(chats),
            "chats": chats,
            # Просроченный код в панели - ловушка: человек диктует его боту, а
            # тот отказывает. Лучше честно попросить показать новый.
            "code": self.code if live and not chats else "",
            "code_stale": bool(self.code) and not live and not chats,
            "error": self.error,
            "has_env_token": bool(os.environ.get("TELEGRAM_TOKEN", "").strip()),
        }

    def _issue_code(self) -> str:
        self.code = self.store.new_code(self.key)
        return self.code

    def _note(self, text: str) -> None:
        """Последняя беда опроса - её показываем в панели, а не только в консоли."""
        self.error = text

    def _halt(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._thread = None
        self._stop = None


RUNNER = TelegramRunner(STORE)


def _say(key: str, text: str, ring: dict | None, days: int, since: int = 0) -> dict:
    """
    Один шаг разговора на странице.

    Выводы в разговоре - только по записям самого человека. Раньше сюда шли
    связи придуманного дневника, и разговор выходил бессвязным: человек пишет
    «ел мясо, пил пиво», а в ответ ему рассказывают про кофе и аврал, которых
    он не называл. Придуманный дневник никуда не делся - он показывается
    целиком по кнопке «Показать все выводы», где сразу сказано, что он для
    показа.

    since - номер последнего сообщения, которое страница уже показала. Отдаём
    ленту от него, а не от начала шага: сообщение из чата, пришедшее до нажатия
    «Отправить», иначе пропадало навсегда - страница ставила lastSeq за него, и
    в /state оно больше не попадало. Повторы страница отбивает сама по номеру.
    """
    diary = STORE.get(key)
    with diary.lock:
        before = diary.seq      # номер до шага: с ним и сверяем чужой since
    dialog.step(diary, text, ring=ring, links=diary.links(), hints=diary.hints(),
                parser=PARSER, origin="page")
    # Лента и её номер - под одним замком, иначе сообщение, легшее между ними,
    # страница не покажет уже никогда.
    with diary.lock:
        return {"messages": diary.feed(_shown(since, before)), "seq": diary.seq}


def _shown(since: int, seq: int) -> int:
    """
    Номер, до которого лента у страницы уже есть. Номер больше нашего - из
    прошлой жизни сервера: программу перезапустили, а вкладка осталась
    открытой со своим счётчиком. Отдавать ей на такой номер пустоту нельзя -
    человек не увидит даже собственного ответа, страница выглядит немой.
    Считаем такой номер нулём и отдаём ленту с начала.

    Сверяться надо с номером ДО шага: после перезапуска первый же ответ
    добирал номера до чужого since, и первая реплика пропадала навсегда.
    """
    return since if since <= seq else 0


def _set_city(key: str, value) -> dict:
    """
    Город человека - всё, что нужно для погоды.

    Название сразу проверяем геокодингом: сказать «такого города не нашлось»
    надо в момент ввода, а не молчанием про погоду через неделю. Но если
    проверка не прошла, город всё равно запоминаем - сети могло не быть, а
    выбрасывать введённое из-за этого нечестно.

    Геокодинг - сетевой вызов, поэтому замок дневника берём после него.
    """
    city = ("" if value is None else str(value))[:MAX_CITY].strip()
    found = bool(city) and weather.coords(city) is not None
    diary = STORE.get(key)
    with diary.lock:
        diary.city = city
    # Город человек называет один раз, спрашивать снова после перезапуска
    # незачем. Отдельного файла под него больше нет: он лежит в дневнике, там
    # же, где записи, - два разных хранилища на одну программу ни к чему.
    diary.save()
    return {"ok": True, "city": city, "known": found}


# Город раньше лежал отдельным файлом рядом со страницей. Теперь он живёт в
# дневнике, вместе с записями: два разных хранилища на одну программу ни к чему.
# Но у того, кто запускал прототип до переезда, старый файл на диске остался, и
# названный им город терять нельзя - переносим его один раз при запуске.
CITY_FILE = Path(__file__).parent / ".city"


def _move_city_from_file() -> None:
    """
    Перенести город из старого файла в дневник. Файл убираем только после того,
    как хранилище подтвердило запись: иначе одна неудача на диске стирает
    единственное место, где город ещё был.
    """
    if not CITY_FILE.exists():
        return
    try:
        city = CITY_FILE.read_text(encoding="utf-8").strip()[:MAX_CITY]
    except OSError:
        return                      # прочитать не вышло - пусть лежит дальше
    diary = STORE.get(WEB_KEY)
    if city and not diary.city:
        with diary.lock:
            diary.city = city
        if not diary.save():
            return
        print(f"Город «{city}» перенесён из web/.city в дневник.")
    try:
        CITY_FILE.unlink()
    except OSError:
        pass


def _weather_today(key: str) -> dict:
    """
    Сегодняшняя погода в городе человека и факторы, которые из неё вышли.

    Нужна панели источников: пока погода была видна только в выводах, человек
    не знал, работает она вообще или нет.

    Города нет, сети нет, сервис молчит - это не ошибка, а обычный ход событий:
    отвечаем пустыми показателями. Пустой `city` значит «город не назвали»,
    пустой `day` - «сегодняшних измерений нет».

    `weather.readings` ходит в сеть, поэтому замок дневника берём только чтобы
    прочитать город, и отпускаем до запроса.
    """
    diary = STORE.get(key)
    with diary.lock:
        city = diary.city

    by_day = weather.readings(city) if city else {}
    today = date.today()
    values = by_day.get(today, {})
    was = by_day.get(today - timedelta(days=1), {})

    # «+7 к вчера» показываем только по тем показателям, что измерены оба дня:
    # разница с неизвестным - это не ноль, это ничто.
    change = {name: round(value - was[name], 1)
              for name, value in values.items() if name in was}
    facts = weather.day_factors(values, was or None) if values else []

    return {
        "ok": True,
        "city": city,
        "source": weather.source_name(),
        "day": today.isoformat() if values else "",
        "today": values,
        "change": change,
        # Что сработало сегодня и что вообще проверялось: без второго списка
        # спокойный день неотличим от дня, про который мы ничего не знаем.
        "factors": [fact.name for fact in facts if fact.value > 0],
        "checked": [fact.name for fact in facts],
        "note": weather.day_note(values, was or None),
    }


def _food_step(key: str, payload: dict, since: int = 0) -> dict:
    """
    Еда числами: КБЖУ за сегодня руками или выгрузка из приложения.

    Отдельным запросом, а не вместе с репликой: числа человек вводит один раз
    за день, а рассказывает про день сколько угодно раз, и таскать их с каждой
    фразой незачем.
    """
    diary = STORE.get(key)
    try:
        by_day = _food_days(payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not by_day:
        return {"ok": False, "error": "Ни одной строки с числами не разобрал. "
                                      "Нужны колонки: дата, калории, белки, жиры, углеводы."}

    with diary.lock:
        known = {record.day for record in diary.timeline.days}
        if len(known | set(by_day)) > MAX_DIARY_DAYS:
            return {"ok": False, "error": "В дневнике уже слишком много дней, "
                                          "новые из выгрузки не помещаются."}
        before = diary.seq
        food.attach(diary.timeline, by_day)
        if len(by_day) == 1:
            day, values = next(iter(by_day.items()))
            when = "за сегодня" if day == date.today() else f"за {day_name(day, date.today())}"
            diary.say("me", f"КБЖУ {when}: {food.told(values)}")
        else:
            first, last = min(by_day), max(by_day)
            diary.say("me", f"Загрузил КБЖУ за {days_count(len(by_day))}: "
                            f"с {first.isoformat()} по {last.isoformat()}")
        diary.say("bot", "Записал. Числа стали факторами дня наравне с привычками.",
                  note=_food_note(by_day))
        # Выгрузка КБЖУ идёт мимо разговора, поэтому и на диск её кладём здесь:
        # в `dialog.step` этот путь не заходит.
        diary.save()
        # Число дней словами считает программа, а не страница: «2 дней» на ней
        # уже выходило, а правил склонения в javascript нет.
        return {"ok": True, "days": len(by_day), "told": days_count(len(by_day)),
                "messages": diary.feed(_shown(since, before)), "seq": diary.seq}


def _food_note(by_day: dict) -> str:
    """
    Что вышло из чисел. Перечисляем только то, что где-то сработало: строка
    «недоел: не было» за каждый день - это шум, а не объяснение.
    """
    names = []
    for values in by_day.values():
        for fact in food.day_factors(values):
            if fact.value > 0 and fact.name not in names:
                names.append(fact.name)
    if not names:
        return ("Ни один порог не сработал: по этим числам день обычный. "
                "Дни без перекоса тоже записаны - без них не с чем сравнивать.")
    return "Факторы из чисел: " + ", ".join(names) + "."


def _food_days(payload: dict) -> dict:
    """Числа по дням из тела запроса: либо выгрузка, либо один день руками."""
    table = payload.get("csv")
    if table is not None:
        if not isinstance(table, str):
            raise ValueError("csv: ожидается текст")
        by_day = food.read_csv(table[:MAX_CSV])
        # Дни из будущего и из прошлого века в дневник не кладём: каждый такой
        # день - новая запись навсегда, и выгрузкой с датами вразброс можно
        # было бы раздувать дневник сколько угодно.
        today = date.today()
        earliest = today - timedelta(days=FOOD_WINDOW_DAYS)
        inside = {day: values for day, values in by_day.items() if earliest <= day <= today}
        if by_day and not inside:
            raise ValueError("Все даты в выгрузке - вне последних двух лет. "
                             "Принимаю КБЖУ только за них.")
        return inside

    values: dict[str, float] = {}
    for name, limit in MAX_FOOD.items():
        value = payload.get(name)
        if value is None or value == "":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"«{name}»: ожидается число")
        # NaN не сравнивается ни с чем, поэтому ловим его отдельно
        if value != value or not 0 <= value <= limit:
            raise ValueError(f"«{name}»: число не похоже на правду")
        values[name] = float(value)
    if not values:
        return {}
    return {date.today(): values}


def _summary_step(key: str, days: int, since: int = 0) -> dict:
    """Все выводы разом. Идут в ту же ленту, что и разговор."""
    ready = _summary(days)      # тяжёлую статистику считаем до замка дневника
    diary = STORE.get(key)
    with diary.lock:
        before = diary.seq
        diary.say("me", "Что уже известно?")
        for message in ready:
            diary.say(message["kind"], message["text"], message.get("note", ""))
        # От номера, который есть у страницы: см. _say
        return {"messages": diary.feed(_shown(since, before)), "seq": diary.seq}


def _summary(days: int) -> list[dict]:
    """
    Все выводы разом.

    Показываем только то, что выдержало проверку. Отсеянные связи раньше шли
    подряд отдельными сообщениями, и получалась бессмыслица: «после кофе
    тревожнее, но виноват аврал», а следом «в аврал спится хуже, но виноват
    кофе». Каждая отменяла соседнюю, а объяснение повторялось дословно по три
    раза. Теперь отсеянное - одной строкой в конце: сам факт отбора важен, а
    перечислять то, во что мы сами не верим, незачем.
    """
    timeline, links = _diary(days)
    real = [l for l in links if l.strength != "наблюдение" and not l.confounder]
    dropped = [l for l in links if l.strength != "наблюдение" and l.confounder]
    named = period_name(days)

    if not real:
        return [{"kind": "bot", "text": f"За {named} ничего надёжного не набралось. "
                                        "Это тоже ответ: случайных совпадений не показываю."}]

    messages = [{
        "kind": "bot",
        "text": f"Придуманный дневник за {named} - на нём видно, как работают выводы. "
                f"Ваши сегодняшние записи в него не входят: за один день выводов не бывает.",
    }]
    # «Совпадением это уже не объяснить» в каждой строке - шум: человек читает
    # это четвёртый раз подряд и перестаёт замечать. Достаточно чисел.
    messages += [{"kind": "result", "text": say(l), "note": _short_basis(l)}
                 for l in real]

    if dropped:
        names = ", ".join(dict.fromkeys(f"«{l.factor}»" for l in dropped))
        messages.append({
            "kind": "bot",
            "text": f"Ещё {days_count(len(dropped)).split()[0]} связи отсеял: {names} "
                    f"выглядели убедительно, но объясняются другими факторами. "
                    f"Показывать их как выводы было бы враньём.",
        })

    # Эксперимент предлагаем только по тому, что человек решает сам. «Уберите
    # аврал на работе» или «отмените перелёт» - совет, который нельзя выполнить.
    best = next((l for l in real if can_try(l.factor)), None)
    if best:
        experiment = Experiment.from_link(best, start=date.today(), days=14)
        messages.append({
            "kind": "bot",
            "text": (f"Так это можно проверить на себе за две недели:\n"
                     + "\n".join(experiment.plan)),
            "note": "Это не задание, а предложение. Скажете «начали» - "
                    "буду считать дни и в конце сравню две половины.",
        })
    return messages


# Сколько дней нужно, чтобы было что сравнивать, и сколько - для настоящего
# вывода. Числа берём из порогов статистики, а не пишем на глаз: первые
# сравнения (догадки) идут от трёх дней с привычкой и трёх без, вывод - от
# семи и семи. Поменяется порог - страница скажет новое число сама.
FIRST_DAYS = 2 * OBSERVATION_MIN_DAYS
SURE_DAYS = 2 * MIN_DAYS_PER_GROUP

# Догадок на странице не больше трёх: длинный список полудогадок учит не
# верить и настоящим выводам (то же правило в today.py - там одна за раз).
MAX_GUESSES = 3


def _finding(link, weak: bool = False, own: bool = True) -> dict:
    """
    Одна находка для карточки на странице. Метка силы - уровень из статистики
    (Link.strength), а не наше впечатление; у догадки - «пока догадка».
    План проверки на себе - только по тому, что человек решает сам (can_try).
    """
    plan = (Experiment.from_link(link, start=date.today(), days=14).plan
            if can_try(link.factor) else [])
    return {"title": headline(link),
            "text": guess(link) if weak else say(link),
            "basis": compared(link, own),
            "tag": "пока догадка" if weak else link.strength,
            "weak": weak, "plan": plan}


def _insights(key: str) -> dict:
    """
    Что Мира заметила в дневнике самого человека: сколько дней записано и
    какие связи нашлись. Связи - те же, что идут в разговор (Diary.links и
    Diary.hints, посчитанные один раз на версию дневника), так что страница и
    разговор не могут друг другу противоречить.
    """
    diary = STORE.get(key)
    links, hints = diary.links(), diary.hints()
    with diary.lock:
        recorded = sum(1 for record in diary.timeline.days if record.facts)
    sure = [l for l in links if l.strength in ("подтверждено", "вероятная связь")]
    # Те же пороги, по которым разговор решает, говорить ли про наблюдение
    guesses = sorted((l for l in hints if not l.confounder
                      and abs(l.effect) >= HINT_EFFECT and l.p_value <= HINT_P),
                     key=lambda l: (l.p_value, -abs(l.effect)))
    return {"days": recorded, "first": FIRST_DAYS, "sure": SURE_DAYS,
            "findings": [_finding(l) for l in sure]
                        + [_finding(l, weak=True) for l in guesses[:MAX_GUESSES]]}


# Готовые ответы /insights/example по срокам. Придуманный дневник один и тот же
# (build с постоянным зерном), так что ответ на срок не меняется до перезапуска.
_EXAMPLES: dict[int, dict] = {}

# Те же ответы на диске, рядом с базой: все сроки считаются минуту-полторы, и
# без файла каждая выкладка начиналась бы с этой минуты «Считаю». Личного в
# файле нет - только выводы придуманного дневника.
EXAMPLE_FILE = DB_FILE.parent / ".example-cache.json"


def _code_mark() -> str:
    """
    Отпечаток кода, от которого зависит пример: модули разбора и статистики,
    генератор придуманного дневника и этот файл. Поменялся код - файл с диска
    не годится: в нём выводы прошлой версии.
    """
    root = Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for path in sorted([*root.glob("wam/*.py"), *root.glob("demo/*.py"),
                        Path(__file__).resolve()]):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_examples(mark: str) -> None:
    """Готовые ответы с диска, если они от этой же версии кода."""
    try:
        saved = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return              # файла нет или он битый - посчитаем заново
    if not isinstance(saved, dict) or saved.get("code") != mark:
        return
    listed = dict(PERIODS)
    for days, answer in (saved.get("examples") or {}).items():
        if days.isdigit() and int(days) in listed and isinstance(answer, dict):
            _EXAMPLES.setdefault(int(days), answer)


def _save_examples(mark: str) -> None:
    """
    Записать посчитанное. Через временный файл: оборванная запись не оставит
    полфайла. Словарь копируем одним вызовом - его может пополнять запрос из
    соседнего потока, а json.dumps по живому словарю падает на его росте.
    """
    temp = EXAMPLE_FILE.with_name(EXAMPLE_FILE.name + ".tmp")
    try:
        temp.write_text(json.dumps({"code": mark, "examples": dict(_EXAMPLES)},
                                   ensure_ascii=False), encoding="utf-8")
        os.replace(temp, EXAMPLE_FILE)
    except OSError as exc:
        # Не записали - не беда: следующий запуск посчитает пример заново
        print(f"Пример на диск не записался: {exc}")


def _example(days: int) -> dict:
    """
    Выводы придуманного дневника - образец того, как будут выглядеть свои.
    Тот же отбор, что в _summary: отсеянное другими факторами - одной строкой.
    """
    ready = _EXAMPLES.get(days)
    if ready is not None:
        return ready
    _, links = _diary(days)
    real = [l for l in links if l.strength != "наблюдение" and not l.confounder]
    dropped = [l for l in links if l.strength != "наблюдение" and l.confounder]
    answer = {"days": days, "period": period_name(days),
              "findings": [_finding(l, own=False) for l in real],
              "dropped": list(dict.fromkeys(l.factor for l in dropped))}
    _EXAMPLES[days] = answer
    return answer


def _warm_examples() -> None:
    """
    Посчитать пример на все сроки из списка заранее, в фоне после запуска.

    В свежем процессе срок считался при первом запросе, секунд по десять, и
    раздел «Что Мира заметила» всё это время показывал «Считаю». Сроки идут по
    одному, начиная с того, что выбран на странице по умолчанию: параллельно
    они только мешали бы друг другу и запросам людей. Запрос, пришедший до
    конца прогрева, ждёт свой срок как раньше - замок срока в _diary общий.

    Посчитанное ложится в EXAMPLE_FILE, и следующий запуск той же версии
    кода берёт его оттуда сразу.
    """
    mark = _code_mark()
    _load_examples(mark)
    for days in [DEFAULT_DAYS] + [d for d, _ in PERIODS if d != DEFAULT_DAYS]:
        if STOP.is_set():
            return
        if days in _EXAMPLES:
            continue
        try:
            _example(days)
        except Exception as exc:
            # Прогрев - удобство, а не условие работы: упал - срок посчитается
            # по первому запросу, как раньше.
            print(f"Пример за {days} дней заранее не посчитался: {trace(exc)}")
            continue
        _save_examples(mark)


def _export(key: str, kind: str) -> tuple[bytes, str, str]:
    """
    Дневник человека одним файлом: тело, тип и имя файла.

    Отдаём записи самого человека, а не придуманный дневник со страницы: в
    файле должно лежать то, что он может принести врачу, а не наш показательный
    пример.

    Это самое личное, что есть в программе, поэтому: файл собирается в памяти и
    уходит в ответ, на диск сервера ничего не пишется, в лог тоже - `Handler`
    молчит про все запросы (`log_message`). Отдаётся только своей странице,
    проверка та же, что у остальных запросов (`_ours`).

    Замок дневника держим на всё время сборки: она короткая, целиком в памяти и
    никуда не ходит, а читать список дней, пока его правит соседний запрос,
    нельзя.
    """
    diary = STORE.get(key)
    with diary.lock:
        if kind == "csv":
            return (export.as_csv(diary.timeline).encode("utf-8"),
                    "text/csv; charset=utf-8", "diary.csv")
        return (export.as_text(diary.timeline).encode("utf-8"),
                "text/plain; charset=utf-8", "diary.txt")


def _short_basis(link) -> str:
    """
    Откуда вывод, без повторов. Полная фраза из phrases.basis() хороша один раз,
    а в списке из четырёх выводов её хвост «Совпадением это уже не объяснить»
    повторяется дословно и превращается в шум.
    """
    source = {"прибор": "по данным кольца",
              "погода": "по погоде"}.get(link.source, "по вашим записям")
    return (f"{days_count(link.days_with)} с этим против {link.days_without} без, "
            f"{source}. {next_step(link)}")


# ── HTTP ──────────────────────────────────────────────────────────────────

# Без входа в режиме server открыто только это: сама страница (она и есть
# экран входа), обмен ссылки от бота на сессию, вход по личному коду и
# «Начать без Telegram». Всё остальное - 401.
OPEN_GET = ("/", "/login", "/link", "/privacy", "/healthz", "/mira-mark.png") + tuple(FONTS)
OPEN_POST = ("/auth/login", "/auth/code", "/auth/new")

# Что можно без согласия: увидеть экран согласия, согласиться, выйти,
# удалить себя и забрать свои записи - доступ к своим данным не должен
# зависеть от нового согласия (после смены текста политики, например).
# Писать в дневник и смотреть его без согласия нельзя.
# Личный код - тоже: это ключ к своему же дневнику, а не запись в него.
NO_CONSENT = ("/state", "/consent", "/auth/logout", "/auth/logout-all",
              "/account/delete", "/export", "/account/code")

# Слово, которым человек подтверждает удаление всего. То же, что в боте.
ERASE_WORD = "УДАЛИТЬ"

# Потолки запросов, кроме разбора речи (он в wam.limits, общий с ботом).
# Вход - с одного адреса: это единственная дверь, куда можно ломиться
# перебором. Погода ходит в чужой сервис - час на человека.
LOGIN_LIMIT = limits.Limiter(10, 60)
WEATHER_LIMIT = limits.Limiter(10, 60 * 60)
# Выводы - самое дорогое, что умеет сервер (перестановочные тесты по всему
# дневнику), КБЖУ - разбор выгрузки до 400 строк, город - запрос к геокодеру.
# Человеку на странице столько не нужно никогда; столько нужно только тому,
# кто хочет положить сервис.
SUMMARY_LIMIT = limits.Limiter(10, 60)
# Раздел «Что Мира заметила» спрашивает выводы при каждом открытии, поэтому
# потолок выше, чем у кнопки: человек ходит по разделам туда-сюда.
INSIGHTS_LIMIT = limits.Limiter(30, 60)
FOOD_LIMIT = limits.Limiter(10, 60)
CITY_LIMIT = limits.Limiter(10, 60 * 60)
# Вход по личному коду: 10 попыток в минуту с адреса и общий потолок на всех
# сразу - против перебора с многих адресов. Общий потолок при атаке закрывает
# вход по коду и честным людям, но лишь на минуту, а дневники остаются целы.
# Каждая попытка с совпавшим отпечатком стоит scrypt, это ещё и защита от
# того, чтобы сервис не заняли одними проверками.
CODE_LIMIT = limits.Limiter(10, 60)
CODE_TOTAL = limits.Limiter(100, 60)
CODE_EVERYONE = "*"                 # ключ общего потолка в CODE_TOTAL
# «Начать без Telegram» - 5 новых дневников в час с адреса. Человеку больше
# не нужно никогда; без потолка скрипт завёл бы тысячи пустых аккаунтов.
CREATE_LIMIT = limits.Limiter(5, 60 * 60)
# И на всех сразу: адресов у скрипта может быть много. Плюс потолок тех, кто
# ещё не согласился, - пустые аккаунты живут сутки, больше 2000 таких разом
# значит, что их штампуют, а не заводят люди.
CREATE_TOTAL = limits.Limiter(100, 60 * 60)
MAX_WAITING_CONSENT = 2000
# Новый код - 5 раз в час на человека: каждый стоит scrypt, а человеку
# больше не нужно никогда.
CODE_CHANGE_LIMIT = limits.Limiter(5, 60 * 60)

# Кто оператор персональных данных - решает владелец, пока не решено.
# Подставляется в текст политики, пустое значение честно видно как пропуск.
OPERATOR = os.environ.get("MIRA_OPERATOR", "").strip()

# Сколько дней хранить аккаунт без входа и без новых записей (решение
# владельца от 11.11.2026 - год). Чистка идёт в фоне раз в сутки.
try:
    RETENTION_DAYS = max(1, int(os.environ.get("MIRA_RETENTION_DAYS", "")
                                or accounts.RETENTION_DAYS))
except ValueError:
    raise SystemExit("MIRA_RETENTION_DAYS: ожидается число дней") from None

# Сколько дней журнал веб-сервера (nginx-proxy) хранит адреса - для текста
# политики. Сам сервис адреса на диск не пишет; срок задаёт настройка прокси.
try:
    PROXY_LOG_DAYS = max(0, int(os.environ.get("MIRA_PROXY_LOG_DAYS", "") or 14))
except ValueError:
    raise SystemExit("MIRA_PROXY_LOG_DAYS: ожидается число дней") from None

# Заголовки каждого ответа в режиме server. no-store - на всё, кроме шрифтов:
# почти каждый ответ несёт чужой дневник, сессию или метку CSRF, а страница
# весит десятки килобайт и от кэша ничего не выигрывает. Общий компьютер или прокси не
# должны сохранять у себя ни строчки. HSTS ставит и nginx-proxy, но сервис
# не должен зависеть от того, как настроен прокси перед ним.
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "Cache-Control": "no-store",
}

# CSP для всего, кроме наших html-страниц: только свой адрес, без плагинов,
# без встраивания в чужие рамки, без подмены <base> и отправки форм наружу.
BASE_POLICY = ("default-src 'self'; base-uri 'none'; object-src 'none'; "
               "frame-ancestors 'none'; form-action 'self'")

_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
_STYLE = re.compile(r"<style>(.*?)</style>", re.S)
_HANDLER = re.compile(r'\son[a-z]+="([^"]*)"')


def _sha(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def page_policy(body: bytes) -> str:
    """
    CSP для html-страницы, посчитанная по её же тексту.

    Скрипт и стили у страницы встроены (<script> и <style> внутри html), а
    кнопки вызывают функции через onclick="...". 'unsafe-inline' разрешил бы
    это одной строкой, но вместе с этим и любой скрипт, который удастся
    подсунуть через innerHTML ленты. Поэтому разрешаем ровно наши куски по
    sha256: блок <script>, блок <style> и тексты обработчиков onclick и
    подобных. Для обработчиков CSP требует ещё 'unsafe-hashes' - оно пускает
    только обработчики с теми же хэшами, то есть те, что уже есть в разметке.

    Считаем на каждый ответ, а не один раз: в скрипт страницы подставляется
    имя бота, а оно становится известно после запуска.
    """
    text = body.decode("utf-8")
    scripts = sorted({_sha(block) for block in _SCRIPT.findall(text)})
    handlers = sorted({_sha(html.unescape(code)) for code in _HANDLER.findall(text)})
    styles = sorted({_sha(block) for block in _STYLE.findall(text)})
    script_src = " ".join(["'self'"] + scripts
                          + (["'unsafe-hashes'"] + handlers if handlers else []))
    style_src = " ".join(["'self'"] + styles)
    return f"{BASE_POLICY}; script-src {script_src}; style-src {style_src}"


POST_PATHS = ("/say", "/summary", "/reset", "/city", "/weather", "/food")
LOCAL_POST = ("/telegram/connect", "/telegram/disconnect",
              "/telegram/code", "/telegram/unlink")
SERVER_POST = ("/auth/login", "/auth/code", "/auth/new", "/auth/logout",
               "/auth/logout-all", "/consent", "/account/delete", "/account/code",
               "/account/telegram", "/account/telegram/confirm")


def need_login() -> str:
    """Что сказать тому, кто не вошёл. Про бота - только если бот есть."""
    if SERVICE_BOT is not None:
        return ("Нужно войти: введите личный код или напишите боту /start или /login.")
    return "Нужно войти: введите личный код или начните дневник заново."


class _Gone(Exception):
    """Сессия живая, а человека уже нет: удалён, пока запрос был в пути."""


class Server(ThreadingHTTPServer):
    """
    Сервер, который не пишет в журнал лишнего. Стандартный handle_error
    печатает весь стек, а в тексте исключения бывают куски того, что человек
    написал про здоровье. Пишем только тип и отпечаток, как бот.
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        # Человек закрыл вкладку, не дождавшись ответа, - не беда сервиса
        if isinstance(exc, ConnectionError):
            return
        print(f"Ошибка запроса: {trace(exc)}")


class Handler(BaseHTTPRequestHandler):
    # Сколько ждать запрос от открытого соединения. Без этого соединение,
    # которое открыли и молчат, держит поток вечно, и сотня таких кладёт сервис.
    timeout = 30
    # Чей это запрос. Заполняет _admit() и только он; до проверки входа
    # _user_key() отказывается называть дневник.
    uid: int | None = None
    sid: str = ""
    # CSP этого ответа: у html-страницы своя, с хэшами её скриптов; у
    # остального - BASE_POLICY. Ставит _send(), отправляет end_headers().
    policy: str = ""
    # Cache-Control этого ответа; пусто - no-store из SECURITY_HEADERS
    cache: str = ""

    def do_GET(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    def _guarded(self, handle) -> None:
        """
        Любая неожиданная ошибка обработчика - честный 500 без подробностей,
        а не оборванное соединение. В журнал - тип и отпечаток, без текста.
        """
        self.answered = False
        try:
            handle()
        except _Gone:
            if not self.answered:
                self._fail(401, need_login())
        except ConnectionError:
            raise                       # клиент ушёл - отвечать некому
        except Exception as exc:
            print(f"Ошибка запроса: {trace(exc)}")
            if not self.answered:
                self.close_connection = True
                self._fail(500, "внутренняя ошибка")

    def send_response(self, code, message=None):
        # Отметка «ответ уже начат»: второй ответ поверх первого испортил бы оба
        self.answered = True
        super().send_response(code, message)

    def _get(self):
        self.uid, self.sid = None, ""
        path = urlparse(self.path).path
        if path == "/healthz":
            # Проверка живости для docker. Стоит до _ours(): docker стучится
            # изнутри контейнера на 127.0.0.1, а не на MIRA_PUBLIC_HOST, и
            # проверку хоста не прошёл бы. Ничего не читает и ничего не отдаёт,
            # кроме «ok», - поэтому открыт и без входа, и снаружи.
            self._send(b"ok", "text/plain; charset=utf-8")
            return
        if not self._ours():
            return
        if path == "/" or (MODE == "server" and path in ("/login", "/link")):
            # Перезагрузка страницы разговор не сбрасывает: для этого есть
            # кнопка «Начать заново». /login - та же страница: её скрипт
            # достаёт токен из адреса после # и меняет его на сессию.
            self._send(page(), "text/html; charset=utf-8")
            return
        if path == "/privacy":
            self._send(privacy(), "text/html; charset=utf-8")
            return
        if path in FONTS:
            # Шрифт - не данные человека, его можно кэшировать: файлы меняются
            # только с выкладкой, неделя - разумный срок.
            self.cache = "public, max-age=604800"
            self._send(FONTS[path].read_bytes(), "font/woff2")
            return
        if path == "/mira-mark.png":
            # Знак виден и на экране входа, поэтому открыт без входа, как шрифты
            self.cache = "public, max-age=604800"
            self._send(MARK.read_bytes(), "image/png")
            return
        if not self._admit() or not self._consented(path):
            return
        if path == "/state":
            self._state()
        elif path in ("/insights", "/insights/example"):
            # Только читает и ничего не меняет, поэтому GET. Считать связи
            # дорого, отсюда потолок на человека.
            if not self._allowed(INSIGHTS_LIMIT, "Выводы спрашивали слишком часто, "
                                                 "подождите минуту.", 60):
                return
            if path == "/insights":
                self._json(_insights(self._user_key()))
            else:
                # Срок - только из списка: любой другой занял бы место в кэше
                # и семь секунд счёта, а чужой запрос мог бы перебрать их все.
                days = _int_param(self.path, "days", DEFAULT_DAYS)
                self._json(_example(days if days in dict(PERIODS) else DEFAULT_DAYS))
        elif path == "/export":
            # Выгрузка ничего не меняет, поэтому GET: страница забирает её
            # обычным fetch и сохраняет файл сама. Чужому сайту она не достанется
            # - его отсеет _ours() по Origin и Sec-Fetch-Site.
            kind = _one_param(self.path, "format")
            body, kind_header, name = _export(self._user_key(), kind)
            # attachment - на случай, если человек откроет адрес руками: иначе
            # браузер покажет дневник страницей вместо того, чтобы сохранить.
            self._send(body, kind_header,
                       extra={"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.send_error(404)

    def _state(self) -> None:
        since = _int_param(self.path, "since")
        if MODE == "server" and not ACCOUNTS.has_consent(self.uid):
            # Без согласия - только то, что нужно экрану согласия: ни ленты,
            # ни города. Метка CSRF нужна, чтобы нажать «Согласен».
            self._send_json({"mode": MODE, "consent_needed": True,
                             "consent_version": CONSENT_VERSION, "csrf": csrf_for(self.sid),
                             "account": _account(self.uid)},
                            extra={"Set-Cookie": _cookie(self.sid)})
            return
        if MODE == "server":
            # Панели бота со страницы в режиме server нет: бот у сервиса один,
            # и человек привязан к нему с первого /start.
            telegram = None
        else:
            # Панель бота спрашиваем до замка дневника: она берёт замок
            # хранилища, а хранилище местами берёт замок дневника под своим -
            # обратный порядок рано или поздно свёл бы два потока намертво.
            telegram = RUNNER.state()
        diary = STORE.get(self._user_key())
        # Лента и номер - одним куском: сообщение из чата, легшее между
        # ними, страница больше никогда не покажет - поставит по нему
        # lastSeq. Заодно feed не перебирает список, который в этот
        # момент подрезает другой поток.
        with diary.lock:
            messages, seq = diary.feed(_shown(since, diary.seq)), diary.seq
            city = diary.city
        # Какой источник погоды работает - видно в панели: у человека с
        # ключом Яндекса и без него погода приходит из разных мест, и
        # понять по цифрам, из какого именно, нельзя.
        state = {"mode": MODE, "messages": messages, "seq": seq,
                 "city": city, "weather_source": weather.source_name(),
                 # Молчаливый откат на словарь - обман: на странице
                 # написано, что речь разбирает модель, а она молчит.
                 "model_error": PARSER.last_error}
        if MODE == "server":
            # Почему молчит модель, видят все люди сразу: разборщик у сервиса
            # один. Текст ошибки бывает с кусками ответа модели, а в нём -
            # чужие слова. Поэтому на сервере - только сам факт.
            state["model_error"] = "модель сейчас не отвечает" if PARSER.last_error else ""
            state["account"] = _account(self.uid)
            # Метка для заголовка X-CSRF: без неё страница не сможет ничего
            # отправить. Отдаём только своей странице - чужую отсеял _ours().
            state["csrf"] = csrf_for(self.sid)
            self._send_json(state, extra={"Set-Cookie": _cookie(self.sid)})
        else:
            state["telegram"] = telegram
            self._json(state)

    def _post(self):
        self.uid, self.sid = None, ""
        if not self._ours():
            return
        path = urlparse(self.path).path
        known = POST_PATHS + (SERVER_POST if MODE == "server" else LOCAL_POST)
        if MODE == "server" and not self._json_body():
            return
        if MODE == "server" and path not in OPEN_POST and not self._admit(csrf=True):
            return
        if (MODE == "server" and path in known and path not in OPEN_POST
                and not self._consented(path)):
            return
        if path not in known:
            self._drain()
            self.send_error(404)
            return

        payload = self._payload()
        if payload is None:
            return                      # ответ уже отправлен

        if path == "/auth/login":
            self._login(payload)
            return
        if path == "/auth/code":
            self._login_by_code(payload)
            return
        if path == "/auth/new":
            self._create()
            return
        if path == "/account/code":
            self._new_code()
            return
        if path == "/account/telegram":
            self._telegram_link()
            return
        if path == "/account/telegram/confirm":
            self._telegram_confirm(payload)
            return
        if path == "/auth/logout":
            ACCOUNTS.close_session(self.sid)
            self._send_json({"ok": True}, extra={"Set-Cookie": _cookie("", forget=True)})
            return
        if path == "/auth/logout-all":
            ACCOUNTS.close_all_sessions(self.uid)
            self._send_json({"ok": True}, extra={"Set-Cookie": _cookie("", forget=True)})
            return
        if path == "/consent":
            if payload.get("agree") is not True:
                self._fail(400, "Без согласия дневник не откроется.")
                return
            ACCOUNTS.agree(self.uid)
            self._json({"ok": True, "version": CONSENT_VERSION})
            return
        if path == "/account/delete":
            if payload.get("confirm") != ERASE_WORD:
                self._fail(400, f"Чтобы удалить всё, впишите слово {ERASE_WORD}.")
                return
            try:
                _erase(self.uid)
            except Exception as exc:
                # Удаление идёт одной транзакцией: раз упало - не удалено
                # ничего, и сказать «готово» нельзя.
                print(f"Удаление не прошло: {trace(exc)}")
                self._fail(500, "Удалить не получилось, ничего не удалено. "
                                "Попробуйте ещё раз через минуту.")
                return
            self._send_json({"ok": True}, extra={"Set-Cookie": _cookie("", forget=True)})
            return

        key = self._user_key()
        if path == "/say":
            try:
                text, ring, days, since = _checked(payload)
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            if MODE == "server":
                refusal = limits.may_parse(self.uid)
                if refusal:
                    self._fail(429, refusal, retry=60)
                    return
            if not DEVICES:
                ring = None         # кольцо не подключено: старая вкладка его ещё шлёт
            self._json(_say(key, text, ring, days, since))
        elif path == "/summary":
            # Выводы дописываются в ленту, то есть меняют состояние. Такое
            # нельзя отдавать по GET: чужая страница вызовет его картинкой.
            if not self._allowed(SUMMARY_LIMIT, "Выводы просили слишком часто, "
                                                "подождите минуту.", 60):
                return
            self._json(_summary_step(key, _clamp_days(payload.get("days")),
                                     _seq(payload.get("since"))))
        elif path == "/reset":
            STORE.get(key).reset()
            self._json({"ok": True})
        elif path == "/city":
            if not self._allowed(CITY_LIMIT, "Город меняли слишком часто, "
                                             "попробуйте через час.", 3600):
                return
            self._json(_set_city(key, payload.get("city")))
        elif path == "/weather":
            # Запрос ходит в сеть за погодой, а не только читает готовое,
            # поэтому POST: чужая страница не должна дёргать его картинкой.
            if MODE == "server" and not WEATHER_LIMIT.allow(self.uid):
                self._fail(429, "Погоду спрашивали слишком часто, попробуйте через час.",
                           retry=3600)
                return
            self._json(_weather_today(key))
        elif path == "/food":
            if not DEVICES:
                self._fail(403, "КБЖУ пока не подключено.")
                return
            if not self._allowed(FOOD_LIMIT, "КБЖУ отправляли слишком часто, "
                                             "подождите минуту.", 60):
                return
            self._json(_food_step(key, payload, _seq(payload.get("since"))))
        elif path == "/telegram/connect":
            self._connect(payload)
        elif path == "/telegram/disconnect":
            RUNNER.disconnect()
            self._json({"ok": True})
        elif path == "/telegram/unlink":
            chat = payload.get("chat")
            if chat is not None and (isinstance(chat, bool) or not isinstance(chat, int)):
                self._fail(400, "chat: ожидается число")
                return
            RUNNER.unlink(chat)
            self._json({"ok": True})
        else:
            self._json({"ok": True, "code": RUNNER.new_code()})

    # ── вход ──────────────────────────────────────────────────────────────
    def _login(self, payload: dict) -> None:
        """
        Обменять токен из ссылки бота на сессию. Токен гаснет при первой же
        попытке, удачной или нет по сроку: второй раз по той же ссылке не
        войти никому, в том числе тому, кто подсмотрел её в переписке.
        """
        if not LOGIN_LIMIT.allow(self._client_ip()):
            self._fail(429, "Слишком много попыток входа. Подождите минуту.", retry=60)
            return
        uid = ACCOUNTS.redeem_login(payload.get("token"))
        if uid is None:
            self._fail(401, "Ссылка не сработала: она живёт десять минут и открывается "
                            "один раз. Напишите боту /login - он пришлёт новую.")
            return
        sid = ACCOUNTS.open_session(uid)
        self._send_json({"ok": True}, extra={"Set-Cookie": _cookie(sid)})

    def _login_by_code(self, payload: dict) -> None:
        """
        Вход по личному коду. Ответ на неверный код один и тот же, есть такой
        код или нет: иначе по ответам можно было бы искать живые коды.
        """
        if not CODE_LIMIT.allow(self._client_ip()) or not CODE_TOTAL.allow(CODE_EVERYONE):
            self._fail(429, "Слишком много попыток входа. Подождите минуту.", retry=60)
            return
        try:
            uid = ACCOUNTS.redeem_code(payload.get("code"))
        except accounts.Busy:
            self._busy()
            return
        if uid is None:
            self._fail(401, "Код не подошёл. Проверьте, что он переписан без ошибок.")
            return
        sid = ACCOUNTS.open_session(uid)
        self._send_json({"ok": True}, extra={"Set-Cookie": _cookie(sid)})

    def _create(self) -> None:
        """
        «Начать без Telegram»: новый человек, его личный код и сразу сессия.
        Код отдаём один раз, в этом ответе, - у нас остаётся только хэш.
        Согласия ещё нет: страница покажет код, а потом экран согласия.
        """
        if not CREATE_LIMIT.allow(self._client_ip()):
            self._fail(429, "С этого адреса уже заводили несколько дневников. "
                            "Попробуйте через час.", retry=3600)
            return
        if (not CREATE_TOTAL.allow(CODE_EVERYONE)
                or ACCOUNTS.waiting_consent() > MAX_WAITING_CONSENT):
            self._fail(429, "Сейчас новых дневников заводят слишком много. "
                            "Попробуйте через час.", retry=3600)
            return
        uid = ACCOUNTS.create()
        try:
            code = ACCOUNTS.issue_code(uid)
        except accounts.Busy:
            # Без кода в этот дневник потом не войти - убираем его сразу
            ACCOUNTS.erase(uid, STORE)
            self._busy()
            return
        sid = ACCOUNTS.open_session(uid)
        self._send_json({"ok": True, "code": code}, extra={"Set-Cookie": _cookie(sid)})

    def _new_code(self) -> None:
        """
        Новый личный код. Прежний гаснет, и вместе с ним все остальные входы
        человека: код меняют, когда боятся, что прежний подсмотрели, - значит,
        и открытые по нему сессии надо закрыть. Эта вкладка остаётся.
        """
        if not self._allowed(CODE_CHANGE_LIMIT, "Код меняли слишком часто, "
                                                "попробуйте через час.", 3600):
            return
        try:
            code = ACCOUNTS.issue_code(self.uid)
        except accounts.Busy:
            self._busy()
            return
        ACCOUNTS.close_other_sessions(self.uid, self.sid)
        self._json({"ok": True, "code": code})

    def _busy(self) -> None:
        self._fail(503, "Сервер сейчас занят, попробуйте через пару секунд.", retry=2)

    def _telegram_confirm(self, payload: dict) -> None:
        """
        Вторая ссылка от бота (/link#t=...). Подтвердить её может только та же
        сессия, что нажала «Подключить Telegram» (см. Accounts.offer_link).
        """
        if SERVICE_BOT is None:
            self._fail(404, "Telegram у этого сервиса не подключён.")
            return
        chat_id, problem = ACCOUNTS.confirm_link(payload.get("token"), self.uid, self.sid)
        if chat_id is None:
            reason = {"has": "Telegram уже подключён.",
                      "taken": "Этот Telegram уже ведёт другой дневник Миры."}.get(
                problem, "Ссылка не сработала: откройте её в том же браузере, где нажали "
                         "«Подключить» в разделе «Мои данные», не позже чем через десять минут.")
            self._fail(409 if problem in ("has", "taken") else 400, reason)
            return
        STORE.attach(chat_id, user_key(self.uid))
        try:
            SERVICE_BOT.linked(chat_id)
        except Exception as exc:
            # Привязка уже есть; не дошло сообщение - бот попросит согласие
            # при первой же записи
            print(f"Сообщение о привязке не ушло: {trace(exc)}")
        self._json({"ok": True})

    def _telegram_link(self) -> None:
        """Одноразовая ссылка на бота, которая привяжет Telegram к этому дневнику."""
        if SERVICE_BOT is None:
            self._fail(404, "Telegram у этого сервиса не подключён.")
            return
        if not BOT_NAME:
            # Бот есть, но имя его при запуске не узнали: ссылку не собрать
            self._fail(503, "Бот сейчас недоступен. Попробуйте позже.", retry=60)
            return
        user = ACCOUNTS.user(self.uid) or {}
        if user.get("tg_id") is not None:
            self._fail(409, "Telegram уже подключён.")
            return
        token = ACCOUNTS.issue_link(self.uid, self.sid)
        self._json({"ok": True, "link": f"https://t.me/{BOT_NAME}?start={token}"})

    def _admit(self, csrf: bool = False) -> bool:
        """
        Пустить запрос к дневнику. В режиме local пускаем всех: дневник один и
        лежит на машине самого человека. В режиме server нужна живая сессия, а
        для запросов, которые что-то меняют, ещё и метка CSRF этой сессии.
        """
        if MODE != "server":
            return True
        sid = self._session_cookie()
        uid = ACCOUNTS.session_user(sid) if sid else None
        if uid is None:
            self._drain()
            self._fail(401, need_login())
            return False
        if csrf and not hmac.compare_digest(self.headers.get("X-CSRF") or "",
                                            csrf_for(sid)):
            self._drain()
            self._fail(403, "Запрос без метки страницы. Обновите страницу.")
            return False
        self.uid, self.sid = uid, sid
        return True

    def _allowed(self, limiter: limits.Limiter, reason: str, retry: int) -> bool:
        """Потолок запросов на человека; в режиме local не нужен - человек один."""
        if MODE != "server" or limiter.allow(self.uid):
            return True
        self._fail(429, reason, retry=retry)
        return False

    def _consented(self, path: str) -> bool:
        """
        К дневнику - только с согласием на нынешний текст. Без него открыты
        лишь экран согласия, выход и удаление себя.
        """
        if MODE != "server" or path in NO_CONSENT or ACCOUNTS.has_consent(self.uid):
            return True
        self._drain()
        self._send_json({"ok": False, "consent": True,
                         "error": "Сначала подтвердите согласие на обработку данных."}, 403)
        return False

    def _client_ip(self) -> str:
        """
        Адрес человека для потолка входов. Заголовкам X-Real-IP и
        X-Forwarded-For верим, только если запрос пришёл из частной сети -
        это nginx-proxy в общей сети docker. Пришёл напрямую из интернета -
        заголовки мог написать кто угодно, берём адрес соединения.
        """
        peer = self.client_address[0]
        try:
            trusted = ipaddress.ip_address(peer).is_private
        except ValueError:
            trusted = False
        if trusted:
            real = (self.headers.get("X-Real-IP") or "").strip()
            if real:
                return _per_network(real)
            # nginx дописывает адрес клиента в конец списка - ему и верим
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if forwarded:
                return _per_network(forwarded)
        return _per_network(peer)

    def _session_cookie(self) -> str:
        """
        Ключ сессии из заголовка Cookie. Разбираем руками: SimpleCookie на
        одной кривой чужой cookie того же домена отказывается читать и все
        остальные, и человек оказывается выкинутым из дневника.
        """
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value.strip()
        return ""

    def _json_body(self) -> bool:
        """
        В режиме server POST принимаем только с JSON в теле. Форма с чужого
        сайта не может поставить такой тип без предварительного запроса, а
        его браузер чужому сайту не одобрит - ещё один заслон от CSRF.
        """
        if self.headers.get_content_type() == "application/json":
            return True
        self._drain()
        self._fail(415, "ожидается application/json")
        return False

    def _user_key(self) -> str:
        """
        Чей дневник открыт в этом запросе. Ключ берётся только отсюда и никогда
        из тела запроса: иначе любой подставит чужой номер и прочтёт чужие
        записи. В режиме local дневник один, страницы.
        """
        if MODE != "server":
            return WEB_KEY
        if self.uid is None:
            # Ошибка в самой программе: обработчик дошёл до дневника, не
            # проверив вход. Лучше упасть, чем отдать чей-то дневник.
            raise RuntimeError("дневник спрошен до проверки входа")
        # Сессия могла пережить своего человека на долю секунды: удаление идёт
        # из бота или с другой вкладки. Без этой проверки запрос в пути завёл
        # бы удалённому человеку новый пустой дневник и записал бы в него.
        if ACCOUNTS.user(self.uid) is None:
            raise _Gone()
        return user_key(self.uid)

    def _connect(self, payload: dict) -> None:
        """
        Подключение бота. Токен можно ввести в поле или взять из окружения -
        во втором случае страница токена вообще не видит.
        """
        if payload.get("from_env"):
            token = os.environ.get("TELEGRAM_TOKEN", "")
        else:
            token = payload.get("token")
            token = "" if token is None else str(token)[:200]
        try:
            username = RUNNER.connect(token)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            # Текст ошибки от Telegram уже без токена, но подстрахуемся ещё раз
            self._json({"ok": False, "error": str(exc).replace(token, "***")})
            return
        self._json({"ok": True, "username": username, "code": RUNNER.code})

    def _ours(self) -> bool:
        """
        Запрос точно от нашей страницы, а не от чужой, открытой в том же
        браузере: иначе любой сайт заберёт код привязки к дневнику.

        В режиме local своя страница - это 127.0.0.1. В режиме server - только
        MIRA_PUBLIC_HOST по https: сервис стоит за nginx-proxy, и другой Host
        значит, что запрос пришёл в обход него или подделан.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        hosts = (PUBLIC_HOST,) if MODE == "server" else LOCAL_HOSTS
        if not host or host not in hosts:
            self._drain()
            self._fail(403, "запрос не с этой страницы")
            return False
        origin = self.headers.get("Origin")
        if origin and not _our_origin(origin):
            self._drain()
            self._fail(403, "запрос с другого сайта")
            return False
        # POST браузер всегда шлёт с Origin. Нет его - значит, запрос собран
        # не браузером на нашей странице, а в режиме server такое не пускаем.
        if MODE == "server" and self.command == "POST" and not origin:
            self._drain()
            self._fail(403, "запрос без адреса страницы")
            return False
        # Origin шлют не всегда: у картинки, скрипта и перехода по ссылке его
        # нет, а запрос всё равно чужой. Про это честно говорит Sec-Fetch-Site:
        # свои запросы - same-origin, набранный в строке адреса - none.
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            self._drain()
            self._fail(403, "запрос с другого сайта")
            return False
        return True

    def _drain(self) -> None:
        """
        Дочитать тело перед отказом. Иначе браузер получит обрыв связи вместо
        ответа и человек не узнает, что случилось, а на живом соединении
        остаток тела разберётся как следующий запрос. Совсем большое тело не
        читаем - дешевле закрыть соединение.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1         # длину не разобрать, читать нечего - закрываемся
        if 0 < length <= MAX_BODY * 2:
            self.rfile.read(length)
        elif length != 0:
            self.close_connection = True

    def _payload(self) -> dict | None:
        """Тело запроса как словарь. При любой беде отвечает сама и отдаёт None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._drain()
            self._fail(400, "непонятная длина запроса")
            return None
        if length < 0 or length > MAX_BODY:
            self._drain()
            self._fail(413, "слишком длинный запрос")
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            # RecursionError - тысячи вложенных скобок: json.loads разбирает их
            # рекурсией и падает на глубине, а это кривой запрос, не наша беда
            self._fail(400, "тело запроса не разобрать")
            return None
        if not isinstance(payload, dict):
            self._fail(400, "ожидается объект")
            return None
        return payload

    def _fail(self, code: int, reason: str, retry: int = 0) -> None:
        """
        Отказ с понятной причиной в теле. В строку статуса русский текст
        писать нельзя: она уходит в latin-1 и запрос падает на кодировке.
        retry - через сколько секунд можно снова (для 429).
        """
        self._send(json.dumps({"ok": False, "error": reason},
                              ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code,
                   {"Retry-After": str(retry)} if retry else None)

    def log_message(self, *args):
        pass

    def _json(self, data):
        self._send_json(data)

    def _send_json(self, data, code: int = 200, extra: dict[str, str] | None = None):
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code, extra)

    def _send(self, body: bytes, content_type: str, code: int = 200,
              extra: dict[str, str] | None = None):
        self.policy = (page_policy(body) if MODE == "server"
                       and content_type.startswith("text/html") else "")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            # Заголовки уходят в latin-1, как и строка статуса: русское слово
            # здесь уронило бы ответ. Поэтому имя файла тут только латиницей,
            # человеческое имя страница ставит сама.
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Здесь, а не в _send: через end_headers идут и ответы send_error
        # (404, 400 на кривой запрос), им заголовки нужны так же.
        if MODE == "server":
            for name, value in SECURITY_HEADERS.items():
                if name == "Cache-Control" and self.cache:
                    value = self.cache
                self.send_header(name, value)
            self.send_header("Content-Security-Policy", self.policy or BASE_POLICY)
        super().end_headers()


LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _our_origin(origin: str) -> bool:
    """Origin нашей страницы: в local - 127.0.0.1, в server - https://MIRA_PUBLIC_HOST."""
    try:
        parsed = urlparse(origin)
        port = parsed.port
    except ValueError:
        return False
    if MODE == "server":
        return (parsed.scheme == "https" and parsed.hostname == PUBLIC_HOST
                and port is None)
    return parsed.hostname in LOCAL_HOSTS


def _per_network(address: str) -> str:
    """
    Кого считать одним «адресом» для потолков. IPv6 сводим к сети /64: её
    обычно выдают одному абоненту целиком, и перебирать адреса внутри неё
    бесплатно - иначе потолок «10 в минуту с адреса» не держал бы ничего.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address[:64]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)      # ::ffff:1.2.3.4 - это адрес IPv4
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _cookie(sid: str, forget: bool = False) -> str:
    """
    Cookie сессии. Приставка __Host- заставляет браузер принять её только с
    Secure, без Domain и с Path=/: поддомен или страница по http её не
    подменят. HttpOnly - скрипт страницы ключа не видит, даже если в неё
    что-то внедрят. SameSite=Lax - чужой сайт не отправит её своим POST.
    """
    age = 0 if forget else SESSION_LIFETIME
    return (f"{COOKIE}={'' if forget else sid}; Path=/; Max-Age={age}; "
            "HttpOnly; Secure; SameSite=Lax")


def _int_param(path: str, name: str, default: int = 0) -> int:
    """Целое из строки запроса; что угодно кривое - значение по умолчанию."""
    values = parse_qs(urlparse(path).query).get(name)
    if not values:
        return default
    try:
        return max(0, int(values[0]))
    except (TypeError, ValueError):
        return default


def _one_param(path: str, name: str, default: str = "") -> str:
    """Первое значение параметра из строки запроса; нет его - пустая строка."""
    values = parse_qs(urlparse(path).query).get(name)
    return values[0] if values else default


def _clamp_days(value, default: int = DEFAULT_DAYS) -> int:
    """
    Срок дневника: только один из PERIODS, всё остальное - срок по умолчанию.
    Страница других и не шлёт, а под каждый срок заводится свой придуманный
    дневник в _CACHE: свободное число открывало бы сотни таких на запросы.
    Infinity в теле json.loads принимает молча, а int() от него бросает
    OverflowError - его тоже считаем «не из списка».
    """
    try:
        days = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return days if days in {known for known, _ in PERIODS} else default


def _seq(value, default: int = 0) -> int:
    """
    Номер последнего показанного сообщения из тела запроса. Кривое значение -
    это не повод отказывать: ноль просто вернёт страницу ленту с начала, а
    повторы она отобьёт сама по номеру.
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _checked(payload: dict) -> tuple[str, dict | None, int, int]:
    """
    Разбор тела запроса /say. Кривые данные - это ошибка запроса, а не сбой
    сервера: строка вместо числа в показаниях кольца раньше доходила до
    float() внутри разбора и роняла ответ пятисоткой.
    """
    text = payload.get("text")
    text = "" if text is None else str(text)
    text = text[:MAX_TEXT]

    days = _clamp_days(payload.get("days"))
    since = _seq(payload.get("since"))

    ring = payload.get("ring")
    if ring is not None:
        if not isinstance(ring, dict):
            raise ValueError("ring: ожидается объект")
        readings = {}
        for key, value in ring.items():
            # Не число - это сломанный запрос, о нём лучше сказать прямо.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"ring: «{key}» должно быть числом")
            # А вот число не по шкале - всего лишь показание, которому мы не
            # верим: выкидываем его одно. Отказ на весь запрос стоил человеку
            # всего рассказа про день из-за одного кривого поля.
            # Диапазон считаем со знаком: по модулю проходил sleep_score -100
            # и превращался в «качество сна -100 из 10». NaN не сравнивается
            # ни с чем, поэтому ловим его отдельно.
            limit = MAX_READING.get(key, DEFAULT_READING)
            if value != value or not 0 <= value <= limit:
                continue
            readings[key] = value
        ring = readings
    return text, ring, days, since


def privacy() -> bytes:
    """
    Политика и текст согласия. ЧЕРНОВИК: оператор и реквизиты не определены,
    текст не проверен юристом. Оператор подставляется из MIRA_OPERATOR; пока
    переменной нет, на странице так и стоит пропуск, а не выдуманное имя.
    """
    text = (Path(__file__).parent / "privacy.html").read_text(encoding="utf-8")
    operator = html.escape(OPERATOR) if OPERATOR else "[оператор уточняется]"
    return (text.replace("{OPERATOR}", operator)
                .replace("{CONSENT_VERSION}", html.escape(CONSENT_VERSION))
                .replace("{RETENTION_DAYS}", str(RETENTION_DAYS))
                .replace("{PROXY_LOG_DAYS}", str(PROXY_LOG_DAYS))
                .encode("utf-8"))


def _account(user_id: int) -> dict:
    """Что странице знать про человека: имя в Telegram, подключён ли он, есть ли код."""
    user = ACCOUNTS.user(user_id) or {}
    return {"username": user.get("username", ""), "bot": BOT_NAME,
            "telegram": user.get("tg_id") is not None,
            "has_code": bool(user.get("has_code")),
            # Есть ли у сервиса бот вообще: без него кнопки про Telegram не нужны
            "bot_on": SERVICE_BOT is not None}


def _erase(user_id: int) -> None:
    """
    Удалить человека целиком. Через бота, если он есть: у бота в памяти ещё
    расписание напоминаний, и его тоже надо почистить.
    """
    if SERVICE_BOT is not None:
        SERVICE_BOT.erase(user_id)
    else:
        ACCOUNTS.erase(user_id, STORE)


# Как часто проверять срок хранения. Срок - месяцы, сутки точности хватает.
SWEEP_EVERY = 24 * 60 * 60


def sweep_idle(now: float | None = None) -> int:
    """
    Удалить аккаунты, в которые не входили и не писали дольше срока хранения,
    и те, кто за неделю так и не дал согласия. Удаление полное, как по кнопке
    «Удалить всё». Заодно стираются дневники без хозяина. Возвращает, сколько
    удалено; в журнал - только число, без номеров людей.
    """
    gone = set(ACCOUNTS.idle(RETENTION_DAYS, now)) | set(ACCOUNTS.unconsented(now=now))
    for user_id in gone:
        _erase(user_id)
    # Дневники без хозяина: запись, легшая в гонке с удалением человека
    orphans = ACCOUNTS.orphans()
    for key in orphans:
        STORE.forget(key)
    return len(gone) + len(orphans)


def _sweeping(stop: threading.Event | None = None) -> None:
    """Фоновая чистка по сроку: сразу при запуске и потом раз в сутки."""
    while True:
        try:
            count = sweep_idle()
            if count:
                print(f"Срок хранения: удалено неактивных аккаунтов - {count}")
        except Exception as exc:
            print(f"Чистка по сроку не прошла: {type(exc).__name__}")
        if stop is None:
            time.sleep(SWEEP_EVERY)
        elif stop.wait(SWEEP_EVERY):
            return


def _check_server_settings() -> None:
    """
    Режим server без своего адреса или без базы не запускаем: без адреса
    нечем сверять Host и Origin, без базы негде хранить сессии - сервис
    поднялся бы, но пускал бы не тех или не пускал никого.
    """
    if not PUBLIC_HOST:
        raise SystemExit("MIRA_MODE=server: задайте MIRA_PUBLIC_HOST, например mira.example.ru")
    if ACCOUNTS is None:
        raise SystemExit(f"MIRA_MODE=server: база {DB_FILE} недоступна, "
                         "проверьте MIRA_DB и права на каталог")
    # Бот необязателен: без токена сервис работает только через сайт. Но
    # токен, заданный с ошибкой, - это опечатка, а не решение обойтись без
    # бота: молча работать без него значит потерять всех, кто пишет боту.
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if token and not token_is_shaped_right(token):
        raise SystemExit("MIRA_MODE=server: TELEGRAM_TOKEN не похож на токен от @BotFather. "
                         "Без бота оставьте переменную пустой.")


def _start_service_bot() -> None:
    """
    Бот сервиса - в том же процессе, отдельным потоком: веб-сервер и опрос
    Telegram делят один дневник в памяти. Второй процесс на тот же файл
    затирал бы чужие дни своей памятью.
    """
    global SERVICE_BOT, BOT_NAME
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    SERVICE_BOT = TelegramBot(token=token, store=STORE, parser=PARSER,
                              accounts=ACCOUNTS, public_url=f"https://{PUBLIC_HOST}")
    try:
        BOT_NAME = SERVICE_BOT.username()
    except Exception as exc:
        # Telegram недоступен при запуске - это не повод не подниматься:
        # опрос будет стучаться сам, а страница просто не назовёт бота.
        print(f"Имя бота не узнал: {SERVICE_BOT._trace(exc)}")
    threading.Thread(target=SERVICE_BOT.run, kwargs={"stop": STOP}, daemon=True).start()


# Сигнал остановки для фоновых потоков: опроса бота и чистки по сроку.
STOP = threading.Event()


def stop_service(server: ThreadingHTTPServer | None = None) -> None:
    """
    Выключение по SIGTERM (docker stop, docker compose up при выкладке).

    Порядок важен: сначала перестаём принимать запросы, потом отпускаем бота
    (он дорабатывает сообщение, которое уже в руках, и подтверждает Telegram
    сделанное), и только потом дописываем дневники и закрываем базу - иначе
    запись, начатая после закрытия, упала бы.

    Запросы, которые сервер уже принял, не ждём: потоки у ThreadingHTTPServer
    фоновые, а самый долгий из них - разбор у модели - может не уложиться в
    stop_grace_period. Их шаги идут транзакциями sqlite, так что оборванный
    запрос базу не портит, а только не запишет свою реплику.
    """
    STOP.set()
    if server is not None:
        server.server_close()
    if SERVICE_BOT is not None:
        SERVICE_BOT.halt(STOP)
    STORE.close()


def main() -> None:
    # Прогрев примера на все сроки: первый ответ не ждёт счёта, а запуск и
    # /healthz не ждут прогрева
    threading.Thread(target=_warm_examples, daemon=True).start()
    if MODE == "server":
        _check_server_settings()
    server = Server((HOST, PORT), Handler)
    if MODE == "server":
        # Каталог с базой - только владельцу процесса. Сам файл sqlite
        # заводит хранилище с правами 0600.
        os.chmod(DB_FILE.parent, 0o700)
        # Один раз затереть то, что удалили до secure_delete (см. scrub_once).
        # До бота и до первого запроса: VACUUM держит базу целиком.
        if STORE.storage.scrub_once():
            print("База переписана: удалённое раньше затёрто")
        if os.environ.get("TELEGRAM_TOKEN", "").strip():
            _start_service_bot()
        else:
            print("TELEGRAM_TOKEN не задан: работаю без бота, вход только через сайт")
        threading.Thread(target=_sweeping, args=(STOP,), daemon=True).start()
        print(f"Мира в режиме server: https://{PUBLIC_HOST}, слушаю {HOST}:{PORT}")
    else:
        # Перенос старого города делаем при запуске программы, а не при
        # импорте: модуль импортируют и тесты, а трогать файлы человека они
        # не должны. В режиме server дневника страницы нет, переносить некуда.
        _move_city_from_file()
        print(f"Откройте http://{HOST}:{PORT} - остановить: Ctrl+C")

    def on_term(signum, frame):
        # serve_forever крутится в этом же потоке, а shutdown() ждёт, пока он
        # выйдет из цикла: вызванный отсюда напрямую, он ждал бы сам себя.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_service(server)
        print("\nОстановлено")


if __name__ == "__main__":
    main()
