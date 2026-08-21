---
name: test-commands-what-affects-me
description: Как прогонять тесты проекта ~/what-affects-me (Мира) — команда, время, состав пакетов
metadata:
  type: project
---

Прогон тестов: `cd ~/what-affects-me && python3 -m pytest -q`.

**Why:** Проект (прототип «Миры») — чистый Python-пакет `wam` + `bot`/`web`/`demo`,
без Makefile и без npm-обвязки; конфиг тестов минимальный, отдельная тестовая
инфраструктура (база, контейнеры, переменные окружения) не нужна.

**How to apply:**
- Полный прогон занимает ~25–26 секунд, 107 тестов (состояние на 2026-08-19), все зелёные.
- Тесты лежат в `~/what-affects-me/tests/`, один файл на модуль:
  bot, causality, dialog, diary, experiments, extract, insights, phrases,
  questions, voice, wearables, web. По точечной правке сначала гоняй
  соответствующий `tests/test_<модуль>.py`, потом весь набор.
- Предупреждений pytest на 2026-08-19 не было; появление warnings summary —
  сигнал о новой зависимости или депрекации, стоит упомянуть в ответе.
- Нестабильных (flaky) тестов пока не замечено.
- `dev`-зависимости: `requirements-dev.txt` (только pytest).
