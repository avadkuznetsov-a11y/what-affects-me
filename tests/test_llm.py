"""
Вызов языковой модели.

Проверяем не сеть, а тело запроса и разбор ответа: именно там дважды ломался
разбор речи молча - сначала на `temperature`, потом на рассуждении, съедавшем
весь запас токенов. Человек в обоих случаях видел «не понял, что записать» и
думал, что программа не умеет разбирать речь.
"""
import json

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
