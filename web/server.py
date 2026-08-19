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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.telegram import TelegramBot, token_is_shaped_right
from demo.generate import build
from wam import dialog
from wam.derive import derive_factors
from wam.diary import DiaryStore
from wam.experiments import Experiment, evaluate
from wam.insights import find_links
from wam.llm import available_engine
from wam.phrases import basis, next_step, say

HOST, PORT = "127.0.0.1", 8765
DEFAULT_DAYS = 120
WEB_KEY = "web"          # ключ дневника, который видит страница

# Речь разбирает модель, если для неё есть ключ в окружении. Ключей в
# репозитории нет: у того, кто скачает код, будет работать разбор по правилам,
# пока он не подставит свой ключ.
ENGINE_NAME, _COMPLETE = available_engine()
PARSER = dialog.Parser(ENGINE_NAME, _COMPLETE)

# Дневник один на процесс: то, что человек написал боту в Telegram, видно на
# странице, и наоборот.
STORE = DiaryStore()

MAX_TEXT = 4000          # длиннее человек за раз не пишет, а разбирать дорого
MAX_BODY = 64 * 1024


def page() -> bytes:
    """
    Разметка страницы лежит рядом, в page.html: держать её строкой внутри
    модуля неудобно - редактор не подсказывает ни в html, ни в javascript.
    """
    html = (Path(__file__).parent / "page.html").read_text(encoding="utf-8")
    return html.replace("__ENGINE__", ENGINE_NAME).encode("utf-8")


# ── состояние разговора ───────────────────────────────────────────────────

_CACHE: dict[int, tuple] = {}
_CACHE_LOCK = threading.Lock()


def _diary(days: int = DEFAULT_DAYS):
    """Придуманный дневник и найденные в нём связи. Считаем один раз на срок."""
    ready = _CACHE.get(days)
    if ready is not None:
        return ready
    with _CACHE_LOCK:
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
            self.code = self.store.new_code(self.key)   # код даём только после getMe
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=bot.run, kwargs={"stop": self._stop, "on_error": self._note},
                daemon=True)
            self._thread.start()
        return username

    def disconnect(self) -> None:
        with self._lock:
            self._halt()
            self._token = ""
            self.username = ""
            self.code = ""
            self.error = ""

    def new_code(self) -> str:
        """Новый код привязки. Без подключённого бота он бессмысленен."""
        with self._lock:
            if not self.connected:
                return ""
            self.code = self.store.new_code(self.key)
            return self.code

    def state(self) -> dict:
        """Что показать в панели. Токена тут нет и быть не может."""
        linked = bool(self.store.linked_chats(self.key))
        return {
            "connected": self.connected,
            "username": self.username,
            "linked": linked,
            "code": "" if linked else self.code,
            "error": self.error,
            "has_env_token": bool(os.environ.get("TELEGRAM_TOKEN", "").strip()),
        }

    def _note(self, text: str) -> None:
        """Последняя беда опроса - её показываем в панели, а не только в консоли."""
        self.error = text

    def _halt(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._thread = None
        self._stop = None


RUNNER = TelegramRunner(STORE)


def _say(text: str, ring: dict | None, days: int) -> dict:
    """
    Один шаг разговора на странице. Вопросы и выводы берём по придуманному
    дневнику: на записях за один день выводов не бывает, а показать, как они
    выглядят, надо сразу.
    """
    _, links = _diary(days)
    diary = STORE.get(WEB_KEY)
    messages = dialog.step(diary, text, ring=ring, links=links, parser=PARSER,
                           origin="page", links_from_demo=True)
    return {"messages": messages, "seq": diary.seq}


def _summary_step(days: int) -> dict:
    """Все выводы разом. Идут в ту же ленту, что и разговор."""
    ready = _summary(days)      # тяжёлую статистику считаем до замка дневника
    diary = STORE.get(WEB_KEY)
    with diary.lock:
        before = diary.seq
        diary.say("me", "Что уже известно?")
        for message in ready:
            diary.say(message["kind"], message["text"], message.get("note", ""))
        return {"messages": diary.feed(before), "seq": diary.seq}


def _summary(days: int) -> list[dict]:
    timeline, links = _diary(days)
    strong = [l for l in links if l.strength != "наблюдение"]
    period = f"{days} дней" if days < 60 else f"{round(days / 30)} месяца"

    if not strong:
        return [{"kind": "bot", "text": f"За {period} ничего надёжного не набралось. "
                                        "Это тоже ответ: случайных совпадений не показываю."}]

    messages = [{
        "kind": "bot",
        "text": f"Показываю придуманный дневник за {period} - он нужен, чтобы было видно, "
                f"как работают выводы. Ваши сегодняшние записи в него не входят: за один день "
                f"выводов не бывает.\n\n"
                f"Проверено {len(links)} пар «привычка - состояние», осталось {len(strong)}, "
                f"из них {sum(1 for l in strong if l.confounder)} объясняются другим фактором.",
    }]
    messages += [{"kind": "result", "text": say(l), "note": f"{basis(l)} {next_step(l)}"}
                 for l in strong]

    best = next((l for l in strong if not l.confounder), None)
    if best:
        window = min(40, max(14, days // 3))
        experiment = Experiment.from_link(best, start=timeline.days[-window].day, days=window)
        verdict = evaluate(experiment, timeline)
        messages.append({"kind": "bot",
                         "text": "Проверка сильнейшей связи:\n" + "\n".join(experiment.plan),
                         "note": verdict.text})
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
        elif path == "/summary":
            self._json(_summary_step(_days_param(self.path)))
        elif path == "/state":
            since = _int_param(self.path, "since")
            diary = STORE.get(WEB_KEY)
            self._json({"telegram": RUNNER.state(),
                        "messages": diary.feed(since), "seq": diary.seq})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._ours():
            return
        path = urlparse(self.path).path
        if path not in ("/say", "/reset", "/telegram/connect",
                        "/telegram/disconnect", "/telegram/code"):
            self.send_error(404)
            return

        payload = self._payload()
        if payload is None:
            return                      # ответ уже отправлен

        if path == "/say":
            try:
                text, ring, days = _checked(payload)
            except ValueError as exc:
                self._fail(400, str(exc))
                return
            self._json(_say(text, ring, days))
        elif path == "/reset":
            STORE.get(WEB_KEY).reset()
            self._json({"ok": True})
        elif path == "/telegram/connect":
            self._connect(payload)
        elif path == "/telegram/disconnect":
            RUNNER.disconnect()
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
            self._fail(403, "запрос не с этой страницы")
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1"):
            self._fail(403, "запрос с другого сайта")
            return False
        return True

    def _payload(self) -> dict | None:
        """Тело запроса как словарь. При любой беде отвечает сама и отдаёт None."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._fail(400, "непонятная длина запроса")
            return None
        if length < 0 or length > MAX_BODY:
            # Тело всё равно надо вычитать, иначе браузер получит обрыв связи
            # вместо ответа и человек не узнает, что случилось. Совсем большое
            # не читаем - тогда просто закрываем соединение.
            if 0 < length <= MAX_BODY * 2:
                self.rfile.read(length)
            else:
                self.close_connection = True
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


def _days_param(path: str, default: int = DEFAULT_DAYS) -> int:
    """Срок из строки запроса. Что угодно кривое - берём срок по умолчанию."""
    values = parse_qs(urlparse(path).query).get("days")
    if not values:
        return default
    return _clamp_days(values[0], default)


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
    """Срок дневника: только целое число в разумных пределах."""
    try:
        return max(14, min(365, int(value)))
    except (TypeError, ValueError):
        return default


def _checked(payload: dict) -> tuple[str, dict | None, int]:
    """
    Разбор тела запроса /say. Кривые данные - это ошибка запроса, а не сбой
    сервера: строка вместо числа в показаниях кольца раньше доходила до
    float() внутри разбора и роняла ответ пятисоткой.
    """
    text = payload.get("text")
    text = "" if text is None else str(text)
    text = text[:MAX_TEXT]

    days = _clamp_days(payload.get("days"))

    ring = payload.get("ring")
    if ring is not None:
        if not isinstance(ring, dict):
            raise ValueError("ring: ожидается объект")
        for key, value in ring.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"ring: «{key}» должно быть числом")
        ring = dict(ring)
    return text, ring, days


def main() -> None:
    threading.Thread(target=_diary, daemon=True).start()   # прогрев, чтобы первый ответ не ждал
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Откройте http://{HOST}:{PORT} - остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
