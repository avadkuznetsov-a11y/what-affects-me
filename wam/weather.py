"""
Погода как ещё один источник фактов о дне.

Человек редко связывает своё состояние с погодой: он помнит, что вчера был
аврал, но не помнит, что за ночь давление упало на десять гектопаскалей. А для
поиска связей это такой же фактор, как кофе или тренировка, - и его не надо
записывать руками, достаточно знать город.

Берём open-meteo: он отдаёт и историю, и сегодняшний день, работает без ключа
и без регистрации. Запрос - обычным urllib из стандартной библиотеки, с тем же
контекстом сертификатов, что и вызовы модели.

Главное правило модуля: погода необязательна. Города не назвали, сети нет,
сервис молчит - возвращаем пустоту, и дневник работает ровно как раньше.
Выдуманных значений тут не появляется никогда: их некому будет отличить от
настоящих, а на них потом строятся выводы.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

from .llm import ssl_context
from .schema import Fact, Timeline

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

SOURCE = "weather"

# Сколько прошлых дней просим за раз. Дневник прототипа живёт недели, история
# глубже ему не нужна, а open-meteo в этом запросе больше 92 дней и не отдаёт.
PAST_DAYS = 60

TIMEOUT = 10
MAX_BYTES = 256 * 1024      # ответ на такой запрос - десяток килобайт

# Погода за день меняется медленно, а спрашивают её на каждую реплику. Держим
# ответ несколько часов: так на разговор из десяти фраз приходится один запрос.
REFRESH = 3 * 60 * 60

# Как поле open-meteo называется у нас. Имена русские, как и всё остальное в
# дневнике: их видно в отладке рядом с фактами человека.
FIELDS = {
    "temperature_2m_max": "температура днём",
    "temperature_2m_min": "температура ночью",
    "pressure_msl_mean": "давление",
    "relative_humidity_2m_mean": "влажность",
    "cloud_cover_mean": "облачность",
}

# ── Пороги ────────────────────────────────────────────────────────────────
# Все они грубые и намеренно такие. Тонкой границы «плохой погоды» не бывает,
# и подгонять её под самочувствие мы не имеем права: фактор либо был, либо
# нет, а есть ли связь с состоянием - решает потом статистика на данных
# конкретного человека.

# Обычное давление на уровне моря - 1013 гПа, суточные качания в один-три
# гектопаскаля идут постоянно и погодой не считаются. Шесть за сутки - это уже
# прошедший фронт, то есть заметная смена погоды.
PRESSURE_JUMP = 6.0

# На восемь гектопаскалей ниже обычного - середина циклона.
LOW_PRESSURE = 1005.0

# Дневной максимум. Двадцать восемь - это жаркий день для средней полосы;
# ровной границы «жары» нет, эта округлена.
HOT = 28.0

# Дневной максимум ниже нуля - день, когда не оттаяло.
COLD = 0.0

# Доля закрытого неба за день. Выше восьмидесяти процентов солнца человек
# практически не видит; граница округлённая.
CLOUDY = 80.0

# Средняя относительная влажность за сутки. Восемьдесят пять процентов - это
# сырой день: туман, морось, мокрый снег.
HUMID = 85.0

_LOCK = threading.Lock()
_PLACES: dict[str, tuple[float, float] | None] = {}
_DAYS: dict[tuple[float, float], tuple[float, dict[date, dict[str, float]]]] = {}


def readings(city: str, timeout: float = TIMEOUT) -> dict[date, dict[str, float]]:
    """
    Погода по дням для города: {день: {показатель: значение}}.

    Пустой словарь - это не ошибка, а обычный случай: города не назвали, сети
    нет, сервис не ответил. Сетевой вызов, поэтому звать до замка дневника.

    Историю всегда берём в open-meteo: связи ищутся по прошлым дням, а открытый
    архив есть только там. Если задан ключ Яндекс.Погоды, сегодняшний день
    поверх истории берём у него - по России он ближе к тому, что человек видел
    за окном.
    """
    city = (city or "").strip()
    if not city:
        return {}
    place = coords(city, timeout)
    if place is None:
        return {}
    days = dict(_daily(place, timeout))

    today = _yandex_today(place, timeout)
    if today:
        days[date.today()] = today
    return days


# ── Яндекс.Погода ─────────────────────────────────────────────────────────
#
# Ключ берётся из окружения, в репозитории его нет. Без ключа модуль работает
# как работал: вся погода из open-meteo. Архива Яндекс на тестовом тарифе не
# отдаёт, поэтому история остаётся за open-meteo, а Яндекс уточняет сегодня.
YANDEX_URL = "https://api.weather.yandex.ru/v2/informers"
YANDEX_KEY_ENV = "YANDEX_WEATHER_KEY"

# Давление Яндекс отдаёт в миллиметрах ртутного столба, а вся остальная
# программа считает в гектопаскалях (так отдаёт open-meteo, в них же заданы
# пороги). Переводим здесь, чтобы дальше по коду единица была одна.
MM_HG_TO_HPA = 1.333224


def _yandex_today(place: tuple[float, float],
                  timeout: float = TIMEOUT) -> dict[str, float]:
    """
    Погода на сейчас от Яндекса. Пустой словарь - ключа нет, сети нет или
    сервис отказал; тогда сегодняшний день просто останется от open-meteo.
    """
    key = os.environ.get(YANDEX_KEY_ENV, "").strip()
    if not key:
        return {}

    answer = _get(YANDEX_URL, {"lat": place[0], "lon": place[1], "lang": "ru_RU"},
                  timeout, headers={"X-Yandex-Weather-Key": key})
    if not answer:
        return {}

    fact = answer.get("fact") or {}
    out: dict[str, float] = {}
    try:
        if fact.get("temp") is not None:
            out["температура днём"] = float(fact["temp"])
            out["температура ночью"] = float(fact["temp"])
        if fact.get("pressure_mm") is not None:
            out["давление"] = round(float(fact["pressure_mm"]) * MM_HG_TO_HPA, 1)
        if fact.get("humidity") is not None:
            out["влажность"] = float(fact["humidity"])
        # Облачность приходит долей от нуля до единицы
        if fact.get("cloudness") is not None:
            out["облачность"] = round(float(fact["cloudness"]) * 100, 1)
    except (TypeError, ValueError):
        return {}
    return out


def coords(city: str, timeout: float = TIMEOUT) -> tuple[float, float] | None:
    """
    Координаты города по названию. None - города не нашли или не дозвонились.

    Ненайденный город запоминаем: человек мог опечататься, и долбить сервис
    одной и той же опечаткой на каждую реплику незачем. А вот сбой связи не
    запоминаем - сеть вернётся, и город найдётся.
    """
    key = city.strip().lower()
    with _LOCK:
        if key in _PLACES:
            return _PLACES[key]

    answer = _get(GEOCODE_URL, {"name": city, "count": 1,
                                "language": "ru", "format": "json"}, timeout)
    if answer is None:
        return None                 # не дозвонились, в другой раз попробуем снова

    results = answer.get("results") or []
    place = None
    if results:
        try:
            place = (float(results[0]["latitude"]), float(results[0]["longitude"]))
        except (KeyError, TypeError, ValueError):
            place = None
    with _LOCK:
        _PLACES[key] = place
    return place


def known_city(city: str) -> bool:
    """
    Отзывался ли сервис на такое название. Нужно странице: сказать человеку
    «такого города не нашлось» сразу, а не молчать и не показывать погоду.
    """
    return coords(city) is not None


def day_factors(today: dict[str, float],
                yesterday: dict[str, float] | None = None) -> list[Fact]:
    """
    Факторы дня из погодных показателей.

    Фактор ставим и когда условия не было (значение 0): без дней «без фактора»
    сравнивать не с чем. А вот про то, чего мы не знаем, не говорим ничего:
    нет вчерашнего давления - нет и суждения о перепаде.
    """
    facts: list[Fact] = []

    def put(name: str, hit: bool) -> None:
        facts.append(Fact("factor", name, 1.0 if hit else 0.0, SOURCE))

    pressure = today.get("давление")
    if pressure is not None:
        put("низкое давление", pressure < LOW_PRESSURE)
        was = (yesterday or {}).get("давление")
        if was is not None:
            put("резкий перепад давления", abs(pressure - was) >= PRESSURE_JUMP)

    warmest = today.get("температура днём")
    if warmest is not None:
        put("жара", warmest >= HOT)
        put("холод", warmest <= COLD)

    clouds = today.get("облачность")
    if clouds is not None:
        put("пасмурно", clouds >= CLOUDY)

    damp = today.get("влажность")
    if damp is not None:
        put("сыро", damp >= HUMID)

    return facts


def attach(timeline: Timeline, by_day: dict[date, dict[str, float]]) -> int:
    """
    Дописать погодные факторы в дни дневника. Возвращает число добавленных
    фактов - по нему видно, что источник вообще сработал.

    Новых дней не заводим: день без единой записи человека сравнивать не с чем,
    а в ленте он выглядел бы как пропущенный.

    Сети тут нет, поэтому звать можно и под замком дневника.
    """
    added = 0
    for record in timeline.days:
        values = by_day.get(record.day)
        if values is None:
            continue
        for fact in day_factors(values, by_day.get(record.day - timedelta(days=1))):
            if record.factor(fact.name) is not None:
                continue        # уже посчитано за этот день
            record.add(fact)
            added += 1
    return added


def _daily(place: tuple[float, float], timeout: float) -> dict[date, dict[str, float]]:
    """Погода по дням для координат. Свежий ответ держим несколько часов."""
    now = time.time()
    with _LOCK:
        cached = _DAYS.get(place)
        if cached is not None and now - cached[0] < REFRESH:
            return cached[1]

    answer = _get(FORECAST_URL, {
        "latitude": place[0], "longitude": place[1],
        "daily": ",".join(FIELDS),
        "past_days": PAST_DAYS, "forecast_days": 1, "timezone": "auto",
    }, timeout)
    if answer is None:
        # Не дозвонились - отдаём то, что лежит с прошлого раза. Устаревшая
        # погода честнее пустоты: это измеренные значения, просто не сегодня.
        with _LOCK:
            cached = _DAYS.get(place)
        return cached[1] if cached else {}

    by_day = _unpack(answer.get("daily") or {})
    with _LOCK:
        _DAYS[place] = (now, by_day)
    return by_day


def _unpack(daily: dict) -> dict[date, dict[str, float]]:
    """
    Ответ open-meteo - это столбцы: список дней и по списку значений на каждый
    показатель. Раскладываем в привычный вид «день -> показатели».
    """
    days = daily.get("time") or []
    out: dict[date, dict[str, float]] = {}
    for index, stamp in enumerate(days):
        try:
            day = date.fromisoformat(str(stamp)[:10])
        except ValueError:
            continue
        values: dict[str, float] = {}
        for field, name in FIELDS.items():
            column = daily.get(field) or []
            if index >= len(column) or column[index] is None:
                continue        # пропуск в измерениях - не повод что-то придумывать
            try:
                values[name] = float(column[index])
            except (TypeError, ValueError):
                continue
        if values:
            out[day] = values
    return out


def _get(url: str, params: dict, timeout: float,
         headers: dict | None = None) -> dict | None:
    """
    Запрос к погодному сервису. None - не получилось, и это нормальный ход
    событий. headers нужны Яндексу: ключ он ждёт заголовком, а не в адресе -
    так он не попадёт ни в чужой лог, ни в историю запросов.

    Ошибку глотаем любую, а не только сетевую: прототип должен запускаться на
    машине без интернета, и падение из-за погоды тут недопустимо. Что именно
    сломалось, для дневника значения не имеет - погоды в этот день просто нет.
    """
    try:
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}", headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl_context()) as response:
            return json.loads(response.read(MAX_BYTES))
    except Exception:
        return None


def forget() -> None:
    """Забыть запомненное. Нужно тестам, чтобы соседние проверки не мешали."""
    with _LOCK:
        _PLACES.clear()
        _DAYS.clear()
