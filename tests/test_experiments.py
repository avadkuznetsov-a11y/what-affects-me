"""Эксперимент: честный вердикт, включая «данных не хватило»."""
from datetime import timedelta

from demo.generate import build
from wam.experiments import Experiment, evaluate
from conftest import FAST_PERMUTATIONS
from wam.insights import find_links


def test_confirms_real_effect():
    timeline = build(days=90)
    link = next(l for l in find_links(timeline, permutations=FAST_PERMUTATIONS) if l.factor == "кофе")
    experiment = Experiment.from_link(link, start=timeline.days[-40].day, days=40)
    verdict = evaluate(experiment, timeline)
    assert verdict.status == "подтвердилось"
    assert verdict.effect < 0


def test_short_window_gives_no_verdict():
    timeline = build(days=90)
    link = next(l for l in find_links(timeline, permutations=FAST_PERMUTATIONS) if l.factor == "кофе")
    experiment = Experiment.from_link(link, start=timeline.days[-6].day, days=6)
    assert evaluate(experiment, timeline).status == "недостаточно данных"


def test_plan_is_human_readable():
    timeline = build(days=90)
    link = next(l for l in find_links(timeline, permutations=FAST_PERMUTATIONS) if l.factor == "кофе")
    experiment = Experiment.from_link(link, start=timeline.days[0].day)
    assert len(experiment.plan) == 3
    assert "кофе" in experiment.hypothesis
