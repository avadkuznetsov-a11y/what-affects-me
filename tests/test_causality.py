"""Причины, а не совпадения: датчики как факторы, третий фактор, направление во времени."""
from demo.generate import build
from wam.derive import derive_factors, DERIVED_FROM
from conftest import FAST_PERMUTATIONS
from wam.insights import find_links


def _links():
    return find_links(derive_factors(build(days=120)), permutations=FAST_PERMUTATIONS)


def test_device_readings_become_factors():
    """Плохой сон с кольца — это причина завтрашней вялости, а не только следствие."""
    link = next(l for l in _links() if l.factor == "мало спал" and l.metric == "энергия")
    assert link.source == "прибор"
    assert link.lag_days >= 1
    assert link.effect < 0


def test_false_link_is_explained_by_third_factor():
    """В аврал пьют кофе и тревожатся. Кофе тут ни при чём, и это должно быть видно."""
    link = next(l for l in _links() if l.factor == "кофе" and l.metric == "тревога")
    assert link.confounder == "аврал"
    assert link.strength == "объясняется другим"


def test_real_link_survives_the_check():
    """Настоящая связь не должна списываться на третий фактор."""
    link = next(l for l in _links() if l.factor == "кофе" and l.metric == "качество сна")
    assert link.confounder == ""
    assert link.strength == "подтверждено"


def test_same_day_link_is_not_called_causal():
    """Одновременные события — неизвестно, что было первым."""
    link = next(l for l in _links() if l.lag_days == 0 and not l.confounder)
    assert "причину так не установить" in link.causal_note


def test_no_tautology_between_metric_and_its_own_factor():
    """«Мало спал» против «качества сна» в тот же день — это одно и то же."""
    for link in _links():
        if DERIVED_FROM.get(link.factor) == link.metric:
            assert link.lag_days >= 1
