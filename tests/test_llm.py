"""
Вызов языковой модели.

Проверяем не сеть, а тело запроса и разбор ответа: именно там дважды ломался
разбор речи молча - сначала на `temperature`, потом на рассуждении, съедавшем
весь запас токенов. Человек в обоих случаях видел «не понял, что записать» и
думал, что программа не умеет разбирать речь.
"""
import io
import json
import urllib.request

import pytest

from wam import llm


def _fake_ask(sent: list, answers: list):
    """Подмена сети: складывает отправленное и отдаёт заготовленные ответы."""
    def ask(body, key, timeout):
        sent.append(body)
        return answers.pop(0)
    return ask


def test_thinking_is_disabled(monkeypatch):
    sent, answers = [], [{"content": [{"type": "text", "text": "{}"}]}]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ-для-теста")
    monkeypatch.setattr(llm, "_ask_claude", _fake_ask(sent, answers))

    assert llm.claude_complete()("разбери это") == "{}"
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert "temperature" not in sent[0]


def test_model_without_thinking_field_is_asked_again(monkeypatch):
    """Ответ 400 на незнакомое поле - спрашиваем без него и с запасом токенов."""
    sent = []
    answers = [None, {"content": [{"type": "text", "text": "готово"}]}]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ-для-теста")
    monkeypatch.setattr(llm, "_ask_claude", _fake_ask(sent, answers))

    assert llm.claude_complete()("разбери это") == "готово"
    assert len(sent) == 2
    assert "thinking" not in sent[1]
    assert sent[1]["max_tokens"] > sent[0]["max_tokens"]


def test_text_block_is_taken_even_after_service_blocks(monkeypatch):
    answer = {"content": [{"type": "thinking", "thinking": "..."},
                          {"type": "text", "text": '{"factors": []}'}]}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ-для-теста")
    monkeypatch.setattr(llm, "_ask_claude", _fake_ask([], [answer]))

    assert llm.claude_complete()("разбери") == '{"factors": []}'


def test_no_key_is_an_honest_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        llm.claude_complete()


def test_no_money_is_not_a_reason_to_ask_twice(monkeypatch):
    """
    Ответ 400 «кончились деньги» - не то же самое, что «модель не знает поля».

    Пока эти два случая были одним, разбор речи на пустом ключе молча уходил
    на словарь: два запроса подряд, оба отказ, и человек видел «не понял, что
    записать» вместо честного «модель недоступна».
    """
    import urllib.error

    calls = []

    def refuse(request, timeout=None, context=None):
        calls.append(request)
        raise urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 400, "Bad Request", {},
            io.BytesIO(json.dumps({"error": {"message":
                "Your credit balance is too low to access the Anthropic API."}
            }).encode()))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ-для-теста")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    with pytest.raises(RuntimeError, match="credit balance"):
        llm.claude_complete()("разбери это")
    assert len(calls) == 1          # второй раз не спрашиваем


def test_unknown_field_is_asked_again(monkeypatch):
    """А вот незнакомое поле - как раз повод спросить второй раз, без него."""
    import urllib.error

    answers = []

    def maybe(request, timeout=None, context=None):
        answers.append(json.loads(request.data))
        if "thinking" in answers[-1]:
            raise urllib.error.HTTPError(
                "https://api.anthropic.com/v1/messages", 400, "Bad Request", {},
                io.BytesIO(json.dumps({"error": {"message":
                    "unexpected field: thinking"}}).encode()))
        return _Response(json.dumps(
            {"content": [{"type": "text", "text": "готово"}]}).encode())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "ключ-для-теста")
    monkeypatch.setattr(urllib.request, "urlopen", maybe)

    assert llm.claude_complete()("разбери это") == "готово"
    assert len(answers) == 2
    assert "thinking" not in answers[1]


class _Response:
    """Минимальный ответ сети: подходит для `with urlopen(...) as response`."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_) -> bool:
        return False
