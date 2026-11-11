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
