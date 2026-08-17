"""
Демонстрация полного цикла: запись → факты → связи → эксперимент → вердикт.

Запуск:  python -m demo.run
Ключи и сеть не нужны.
"""
from __future__ import annotations

from datetime import date, timedelta

from demo.generate import build
from wam.experiments import Experiment, evaluate
from wam.extract import RuleExtractor
from wam.insights import find_links, summarise
from wam.wearables import SberRingSource, merge_into

LINE = "─" * 64


def main() -> None:
    print(LINE)
    print("1. Человек рассказывает о дне обычными словами")
    print(LINE)

    extractor = RuleExtractor()
    text = ("Опять пил кофе часов в пять вечера, потом до ночи листал ленту. "
            "Спал часов пять, с утра голова тяжёлая, тревога какая-то.")
    record = extractor.extract(text, date(2026, 8, 1))
    print(f"Запись: {text}\n")
    for fact in record.facts:
        print(f"  · {fact.kind:6} {fact.name:18} {fact.value}")

    print()
    print(LINE)
    print("2. Данные Умного кольца добавляются к рассказу")
    print(LINE)

    timeline = build(days=90)
    ring = SberRingSource()
    readings = ring.read([
        {"date": "2026-08-01", "sleep_score": 41, "stress_level": 72, "energy": 38},
        {"date": "2026-08-02", "sleep_score": 78, "stress_level": 30, "energy": 71},
    ])
    for reading in readings:
        print(f"  {reading.day}: {reading.metrics}")
    merge_into(timeline, readings)

    print()
    print(LINE)
    print("3. Что нашлось за 90 дней дневника")
    print(LINE)
    print(f"Сводка: {summarise(timeline)}\n")

    links = find_links(timeline)
    for link in links:
        print(f"  {link.describe()}")

    if not links:
        print("  Пока ничего надёжного — данных мало.")
        return

    print()
    print(LINE)
    print("4. Из связи рождается эксперимент")
    print(LINE)

    strongest = links[0]
    start = timeline.days[-30].day
    experiment = Experiment.from_link(strongest, start=start, days=28)
    print(f"Гипотеза: {experiment.hypothesis}")
    for step in experiment.plan:
        print(f"  · {step}")

    print()
    print(LINE)
    print("5. Вердикт по итогам наблюдения")
    print(LINE)
    verdict = evaluate(experiment, timeline)
    print(f"Статус: {verdict.status}")
    print(verdict.text)


if __name__ == "__main__":
    main()
