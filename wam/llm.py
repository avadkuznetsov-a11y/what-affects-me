"""
Адаптер языковой модели.

Модель нужна в двух местах: разобрать рассказ на факты и объяснить найденную
связь словами. Провайдер меняется одной строкой — для российского контура это
GigaChat, для локальной разработки хватает заглушки без сети.
"""
from __future__ import annotations

import json
import os
from typing import Callable

Complete = Callable[[str], str]


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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        return payload["choices"][0]["message"]["content"]

    return complete


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
