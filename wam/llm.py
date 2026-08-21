"""
Адаптер языковой модели.

Модель нужна в двух местах: разобрать рассказ на факты и объяснить найденную
связь словами. Провайдер меняется одной строкой — для российского контура это
GigaChat, для локальной разработки хватает заглушки без сети.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable

Complete = Callable[[str], str]


def ssl_context():
    """
    Контекст проверки сертификатов.

    Python, поставленный не из системы, часто идёт без корневых сертификатов -
    тогда любой https-запрос падает с CERTIFICATE_VERIFY_FAILED, хотя curl на
    той же машине работает. Ищем набор сертификатов в привычных местах; если
    ничего не нашли, отдаём обычный контекст и пусть ошибка будет честной.
    """
    import ssl

    candidates = []
    try:
        import certifi

        candidates.append(certifi.where())
    except ImportError:
        pass
    candidates += ["/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem"]

    for path in candidates:
        if path and os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def offline_complete(prompt: str) -> str:
    """Заглушка для разработки и тестов: возвращает пустой разбор."""
    return json.dumps({"factors": [], "metrics": [], "events": []}, ensure_ascii=False)


def gigachat_complete(model: str = "GigaChat", timeout: int = 30) -> Complete:
    """
    Вызов GigaChat. Ключ берётся из переменной окружения GIGACHAT_TOKEN,
    в репозитории секретов нет. Импорт внутри функции — чтобы пакет
    оставался устанавливаемым без сетевых зависимостей.
    """
    token = os.environ.get("GIGACHAT_TOKEN", "")
    if not token:
        raise RuntimeError("не задан GIGACHAT_TOKEN")

    def complete(prompt: str) -> str:
        import urllib.request

        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        }).encode()
        request = urllib.request.Request(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            payload = json.loads(response.read())
        return payload["choices"][0]["message"]["content"]

    return complete


def yandexgpt_complete(model: str = "yandexgpt-lite", timeout: int = 30) -> Complete:
    """
    Вызов YandexGPT. Ключ и каталог читаются из окружения: YANDEX_API_KEY и
    YANDEX_FOLDER_ID. В репозитории их нет и быть не должно - тот, кто
    скачает код, получит разбор по правилам, пока не подставит свой ключ.
    """
    key = os.environ.get("YANDEX_API_KEY", "")
    folder = os.environ.get("YANDEX_FOLDER_ID", "")
    if not key or not folder:
        raise RuntimeError("не заданы YANDEX_API_KEY и YANDEX_FOLDER_ID")

    def complete(prompt: str) -> str:
        import urllib.request

        body = json.dumps({
            "modelUri": f"gpt://{folder}/{model}",
            "completionOptions": {"temperature": 0.1, "maxTokens": 600},
            "messages": [{"role": "user", "text": prompt}],
        }).encode()
        request = urllib.request.Request(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Api-Key {key}"},
        )
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            payload = json.loads(response.read())
        return payload["result"]["alternatives"][0]["message"]["text"]

    return complete


# По каким словам в ответе 400 понятно, что дело в самом запросе, а не в
# ключе или деньгах. Всё остальное - не повод спрашивать второй раз.
_ABOUT_REQUEST = re.compile(
    r"unexpected|unsupported|not supported|invalid.*(field|parameter|property)|"
    r"extra input|thinking|max_tokens|temperature", re.IGNORECASE)


def _ask_claude(body: dict, key: str, timeout: int) -> dict | None:
    """
    Один запрос к Claude. None - сервис не принял тело запроса (ответ 400):
    значит, поле из тела этой модели незнакомо и надо спросить иначе.
    Остальные ошибки не глотаем: их обрабатывает тот, кто звал разбор.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl_context()) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code != 400:
            raise
        # Ответ 400 бывает двух разных смыслов, и путать их дорого: одно дело
        # «модель не знает такого поля» - тогда спрашиваем иначе, другое дело
        # «кончились деньги на ключе» - тогда повтор бессмыслен, а молчаливый
        # откат на словарь выглядит как поломка разбора речи.
        detail = ""
        try:
            detail = json.loads(error.read()).get("error", {}).get("message", "")
        except Exception:
            pass
        if detail and not _ABOUT_REQUEST.search(detail):
            raise RuntimeError(detail) from error
        return None


def claude_complete(model: str = "claude-sonnet-5", timeout: int = 30) -> Complete:
    """
    Вызов Claude. Ключ читается из ANTHROPIC_API_KEY. Обращаю внимание: из
    России этот адрес может быть недоступен, тогда сработает следующий движок.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("не задан ANTHROPIC_API_KEY")

    def complete(prompt: str) -> str:
        # temperature не задаём: свежие модели её не принимают и отвечают 400,
        # а разбор молча откатывается на правила - человек видит «не понял, что
        # записать» и думает, что программа не умеет разбирать речь.
        #
        # Рассуждение выключаем явно. Свежие модели рассуждают по умолчанию, и
        # на разбор дневниковой записи весь запас токенов уходил в размышления:
        # ответ приходил одним блоком thinking, текста в нём не было вовсе, и
        # разбор молча падал на словарь. Со стороны это и выглядело «моделью,
        # которая через раз не понимает». Думать тут не над чем - нужен JSON.
        payload_out = {
            "model": model,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "disabled"},
        }
        payload = _ask_claude(payload_out, key, timeout)
        if payload is None:
            # Модель этого поля не знает - спрашиваем без него, но с запасом
            # токенов, чтобы после размышлений хватило и на сам ответ.
            second = {k: v for k, v in payload_out.items() if k != "thinking"}
            second["max_tokens"] = 4000
            payload = _ask_claude(second, key, timeout) or {}
        # Ответ приходит списком блоков, и текстовый в нём не обязательно первый:
        # свежие модели кладут перед ним свои служебные. Брать content[0] вслепую
        # нельзя - на таком ответе разбор падал и молча уходил на словарь, а
        # человек видел «не понял, что записать» на понятную фразу.
        blocks = payload.get("content") or []
        texts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
        return "\n".join(t for t in texts if t)

    return complete


# Как включить свою модель - это же показывается на странице прототипа
ENGINE_HINTS = (
    ("GigaChat", "GIGACHAT_TOKEN"),
    ("YandexGPT", "YANDEX_API_KEY и YANDEX_FOLDER_ID"),
    ("Claude", "ANTHROPIC_API_KEY"),
)


def available_engine() -> tuple[str, Complete]:
    """
    Что доступно прямо сейчас: модель, если для неё есть ключ, иначе правила.
    Правила - не заглушка, а страховка: продукт не должен вставать из-за
    недоступного сервиса.
    """
    for name, factory in (("GigaChat", gigachat_complete), ("YandexGPT", yandexgpt_complete),
                          ("Claude", claude_complete)):
        try:
            return name, factory()
        except RuntimeError:
            continue
    return "правила", offline_complete


EXPLAIN_PROMPT = (
    "Объясни человеку найденную закономерность из его дневника. Пиши коротко, "
    "по-человечески, без медицинских утверждений и без советов из интернета. "
    "Обязательно скажи, что это пока наблюдение на его данных, и предложи "
    "проверить его экспериментом.\n\nЗакономерность: "
)


def explain(link_description: str, complete: Complete = offline_complete) -> str:
    """Пересказ связи живым языком. Цифры уже посчитаны, модель их не трогает."""
    text = complete(EXPLAIN_PROMPT + link_description).strip()
    return text or link_description
