"""
Локальная страница-диалог: так же, как продукт будет работать в мессенджере.

Запуск:  python3 -m web.server
Откроется на http://127.0.0.1:8765 - ни ключей, ни интернета не нужно.

Человек пишет про свой день, программа отвечает: что записала, чего не хватает
и что уже известно. Когда данных достаточно, показывает выводы. Сервер слушает
только 127.0.0.1 и наружу ничего не отдаёт.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.telegram import TelegramBot, token_is_shaped_right
from demo.generate import build
from wam import dialog, food, weather
from wam.derive import derive_factors
from wam.diary import DiaryStore
from wam.storage import DIARY_FILE
from wam.experiments import Experiment, can_try, evaluate
from wam.insights import find_links
from wam.llm import available_engine
from wam.phrases import basis, days_count, next_step, period, say
from wam.questions import day_name

HOST, PORT = "127.0.0.1", 8765
DEFAULT_DAYS = 120
WEB_KEY = "web"          # ключ дневника, который видит страница

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

# Речь разбирает модель, если для неё есть ключ в окружении. Ключей в
# репозитории нет: у того, кто скачает код, будет работать разбор по правилам,
# пока он не подставит свой ключ.
ENGINE_NAME, _COMPLETE = available_engine()
PARSER = dialog.Parser(ENGINE_NAME, _COMPLETE)

# Дневник один на процесс: то, что человек написал боту в Telegram, видно на
# странице, и наоборот. Лежит он в файле рядом с программой и переживает
# перезапуск - человек ведёт дневник неделями.
STORE = DiaryStore(DIARY_FILE)

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
    html = (html.replace("__ENGINE__", ENGINE_NAME)
                .replace("__PERIODS__", periods)
                .replace("__DEFAULT_DAYS__", str(DEFAULT_DAYS)))
    return html.encode("utf-8")


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


def _say(text: str, ring: dict | None, days: int, since: int = 0) -> dict:
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
    diary = STORE.get(WEB_KEY)
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


def _set_city(value) -> dict:
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
    diary = STORE.get(WEB_KEY)
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


def _weather_today() -> dict:
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
    diary = STORE.get(WEB_KEY)
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


def _food_step(payload: dict, since: int = 0) -> dict:
    """
    Еда числами: КБЖУ за сегодня руками или выгрузка из приложения.

    Отдельным запросом, а не вместе с репликой: числа человек вводит один раз
    за день, а рассказывает про день сколько угодно раз, и таскать их с каждой
    фразой незачем.
    """
    diary = STORE.get(WEB_KEY)
    try:
        by_day = _food_days(payload)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not by_day:
        return {"ok": False, "error": "Ни одной строки с числами не разобрал. "
                                      "Нужны колонки: дата, калории, белки, жиры, углеводы."}

    with diary.lock:
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
        return food.read_csv(table[:MAX_CSV])

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


def _summary_step(days: int, since: int = 0) -> dict:
    """Все выводы разом. Идут в ту же ленту, что и разговор."""
    ready = _summary(days)      # тяжёлую статистику считаем до замка дневника
    diary = STORE.get(WEB_KEY)
    with diary.lock:
        before = diary.seq
        diary.say("me", "Что уже известно?")
        for message in ready:
            diary.say(message["kind"], message["text"], message.get("note", ""))
        # От номера, который есть у страницы: см. _say
        return {"messages": diary.feed(_shown(since, before)), "seq": diary.seq}


def _summary(days: int) -> list[dict]:
    timeline, links = _diary(days)
    strong = [l for l in links if l.strength != "наблюдение"]
    named = period_name(days)

    if not strong:
        return [{"kind": "bot", "text": f"За {named} ничего надёжного не набралось. "
                                        "Это тоже ответ: случайных совпадений не показываю."}]

    messages = [{
        "kind": "bot",
        "text": f"Показываю придуманный дневник за {named} - он нужен, чтобы было видно, "
                f"как работают выводы. Ваши сегодняшние записи в него не входят: за один день "
                f"выводов не бывает.\n\n"
                f"Проверено {len(links)} пар «привычка - состояние», осталось {len(strong)}, "
                f"из них {sum(1 for l in strong if l.confounder)} объясняются другим фактором.",
    }]
    messages += [{"kind": "result", "text": say(l), "note": f"{basis(l)} {next_step(l)}"}
                 for l in strong]

    # Эксперимент предлагаем только по тому, что человек решает сам. «Уберите
    # аврал на работе» или «отмените перелёт» - совет, который нельзя выполнить.
    best = next((l for l in strong if not l.confounder and can_try(l.factor)), None)
    if best:
        # План - это предложение на будущее, и раньше рядом с ним печатался
        # вердикт по прошлым дням. Получалась бессмыслица: сверху «совпадением
        # это уже не объяснить», снизу «пока рано делать вывод», и непонятно,
        # к чему относится и то и другое. Оставляем только предложение.
        experiment = Experiment.from_link(best, start=date.today(), days=14)
        messages.append({
            "kind": "bot",
            "text": (f"Так это можно проверить на себе за две недели:\n"
                     + "\n".join(experiment.plan)),
            "note": "Это не задание, а предложение. Скажете «начали» - "
                    "буду считать дни и в конце сравню две половины.",
        })
    return messages


# ── HTTP ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self._ours():
            return
        path = urlparse(self.path).path
        if path == "/":
            # Перезагрузка страницы разговор не сбрасывает: для этого есть
            # кнопка «Начать заново».
            self._send(page(), "text/html; charset=utf-8")
        elif path == "/state":
            since = _int_param(self.path, "since")
            # Панель бота спрашиваем до замка дневника: она берёт замок
            # хранилища, а хранилище местами берёт замок дневника под своим -
            # обратный порядок рано или поздно свёл бы два потока намертво.
            telegram = RUNNER.state()
            diary = STORE.get(WEB_KEY)
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
            self._json({"telegram": telegram, "messages": messages, "seq": seq,
                        "city": city, "weather_source": weather.source_name()})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._ours():
            return
        path = urlparse(self.path).path
        if path not in ("/say", "/summary", "/reset", "/city", "/weather", "/food",
                        "/telegram/connect", "/telegram/disconnect",
                        "/telegram/code", "/telegram/unlink"):
            self._drain()
            self.send_error(404)
            return

        payload = self._payload()
        if payload is None:
            return                      # ответ уже отправлен

        if path == "/say":
            try:
                text, ring, days, since = _checked(payload)
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            self._json(_say(text, ring, days, since))
        elif path == "/summary":
            # Выводы дописываются в ленту, то есть меняют состояние. Такое
            # нельзя отдавать по GET: чужая страница вызовет его картинкой.
            self._json(_summary_step(_clamp_days(payload.get("days")),
                                     _seq(payload.get("since"))))
        elif path == "/reset":
            STORE.get(WEB_KEY).reset()
            self._json({"ok": True})
        elif path == "/city":
            self._json(_set_city(payload.get("city")))
        elif path == "/weather":
            # Запрос ходит в сеть за погодой, а не только читает готовое,
            # поэтому POST: чужая страница не должна дёргать его картинкой.
            self._json(_weather_today())
        elif path == "/food":
            self._json(_food_step(payload, _seq(payload.get("since"))))
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
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._drain()
            self._fail(403, "запрос не с этой страницы")
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            self._drain()
            self._fail(403, "запрос с другого сайта")
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._fail(400, "тело запроса не разобрать")
            return None
        if not isinstance(payload, dict):
            self._fail(400, "ожидается объект")
            return None
        return payload

    def _fail(self, code: int, reason: str) -> None:
        """
        Отказ с понятной причиной в теле. В строку статуса русский текст
        писать нельзя: она уходит в latin-1 и запрос падает на кодировке.
        """
        self._send(json.dumps({"ok": False, "error": reason},
                              ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def log_message(self, *args):
        pass

    def _json(self, data):
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send(self, body: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _int_param(path: str, name: str, default: int = 0) -> int:
    """Целое из строки запроса; что угодно кривое - значение по умолчанию."""
    values = parse_qs(urlparse(path).query).get(name)
    if not values:
        return default
    try:
        return max(0, int(values[0]))
    except (TypeError, ValueError):
        return default


def _clamp_days(value, default: int = DEFAULT_DAYS) -> int:
    """
    Срок дневника: только целое число в разумных пределах. Infinity в теле
    запроса json.loads принимает молча, а int() от него бросает OverflowError -
    и запрос обрывался без ответа вместо честного «беру срок по умолчанию».
    """
    try:
        return max(14, min(365, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


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


def main() -> None:
    # Перенос старого города делаем при запуске программы, а не при импорте:
    # модуль импортируют и тесты, а трогать файлы человека они не должны.
    _move_city_from_file()
    threading.Thread(target=_diary, daemon=True).start()   # прогрев, чтобы первый ответ не ждал
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Откройте http://{HOST}:{PORT} - остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
