"""
Ограничение частоты запросов: сколько раз за окно времени можно что-то делать.

Нужно режиму server. Разбор реплики моделью стоит денег, погода ходит в чужой
сервис, а вход по ссылке - единственная дверь, в которую можно ломиться
перебором. Без потолка один человек (или скрипт) съедает бюджет и место в
очереди у всех остальных.

Всё в памяти процесса: после перезапуска счёт начинается заново. Для защиты
от перебора и случайного шквала этого достаточно, а хранить чужие адреса и
отметки времени на диске незачем.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

# Сколько разных «кто» держим. Дальше чистим тех, у кого окно уже пустое:
# иначе поток запросов с разных адресов съел бы память счётчиками.
MAX_KEYS = 10_000


class Limiter:
    """Скользящее окно: не больше `limit` событий за `window` секунд на каждого."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._lock = threading.Lock()
        self._seen: dict[object, deque] = {}

    def allow(self, who, now: float | None = None) -> bool:
        """Можно ли ещё раз. Да - событие засчитано; нет - не засчитано."""
        now = time.monotonic() if now is None else now
        edge = now - self.window
        with self._lock:
            times = self._seen.get(who)
            if times is None:
                if len(self._seen) >= MAX_KEYS:
                    self._sweep(edge)
                times = self._seen[who] = deque()
            while times and times[0] <= edge:
                times.popleft()
            if len(times) >= self.limit:
                return False
            times.append(now)
            return True

    def _sweep(self, edge: float) -> None:
        for who in [who for who, times in self._seen.items()
                    if not times or times[-1] <= edge]:
            del self._seen[who]


# ── разбор речи моделью ──────────────────────────────────────────────────
# Решение владельца от 11.11.2026: не больше 20 разборов в минуту и 300 в
# сутки на человека, в сумме со страницы и из бота. Суточный потолок меняется
# переменной MIRA_DAILY_LIMIT. Сутки считаем скользящими, а не календарными:
# так нет полуночи, после которой скрипт получает разом весь новый запас.
def _daily_limit() -> int:
    try:
        return max(1, int(os.environ.get("MIRA_DAILY_LIMIT", "") or 300))
    except ValueError:
        return 300


SAY_PER_MINUTE = Limiter(20, 60)
SAY_PER_DAY = Limiter(_daily_limit(), 24 * 60 * 60)


def may_parse(user_id: int) -> str:
    """
    Можно ли разобрать ещё одну реплику человека. Пустая строка - можно;
    иначе - что ему ответить. Суточный счёт трогаем только если минутный
    пропустил: отказ по минуте не должен съедать суточный запас.
    """
    if not SAY_PER_MINUTE.allow(user_id):
        return "Слишком много сообщений подряд. Подождите минуту и напишите снова."
    if not SAY_PER_DAY.allow(user_id):
        return ("На сегодня записей уже очень много, больше я сегодня не разберу. "
                "Продолжим завтра.")
    return ""
