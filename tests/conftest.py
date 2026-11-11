"""
Общее для тестов.

Перестановочный тест - самая дорогая часть движка: на дневнике в четыре месяца
две тысячи перемешиваний на каждую пару складываются в минуты. Тесты проверяют
не точность p-значения, а поведение: настоящая связь найдена, случайная названа
наблюдением. Для этого хватает и двух сотен перемешиваний, а прогон становится
в десять раз короче. В самом продукте остаётся честное значение по умолчанию.

Здесь же дневник уводится в сторону от настоящего файла человека. `web/server.py`
заводит хранилище прямо при импорте, поэтому подменить путь надо до того, как
тест впервые импортирует сервер: конфтест pytest читает раньше самих тестов.
Каталог временный и уходит вместе с процессом.
"""
import tempfile
from pathlib import Path

import wam.storage

FAST_PERMUTATIONS = 200

_TEMP = tempfile.TemporaryDirectory(prefix="mira-tests-")
wam.storage.DIARY_FILE = Path(_TEMP.name) / "diary.db"


import pytest

import wam.limits


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    """
    Потолки разбора речи живут в памяти модуля и общие на процесс. Без сброса
    один и тот же номер человека из разных тестов копил бы счёт и упирался в
    потолок посреди чужого теста.
    """
    monkeypatch.setattr(wam.limits, "SAY_PER_MINUTE", wam.limits.Limiter(20, 60))
    monkeypatch.setattr(wam.limits, "SAY_PER_DAY",
                        wam.limits.Limiter(wam.limits._daily_limit(), 24 * 60 * 60))
