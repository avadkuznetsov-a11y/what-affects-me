"""
Приборы: показания кольца превращаются в причины, а не в украшение.

Разбор речи и поиск связей проверяются отдельными прогонами. Здесь третья
часть, где ошибиться проще всего и заметить труднее всего: цифры с прибора.
Человек их не пишет сам, глазами не сверяет и поймать вранье не может - тем
важнее проверять машиной.

Что проверяется на дневниках с заранее известной правдой:

- **показания становятся факторами** - «мало спал» и «высокий пульс» из чисел,
  а не из слов, и в правильную сторону;
- **своя норма** - у человека с всегда низкой вариабельностью пульса не должно
  каждый день гореть «организм не восстановился»: важна не сама цифра, а
  отклонение от его обычного;
- **связь по прибору находится** и помечена как пришедшая с прибора;
- **тавтологии нет** - «мало спал» против «качества сна» в тот же день это
  один показатель с двух сторон, и такой связи быть не должно;
- **день не уезжает** - показания за сутки лежат в своих сутках;
- **в разговоре** цифры с ползунков попадают в сегодняшнюю запись и человек
  видит пометку «с кольца», а не молчаливую подмену своих слов.

    python3 -m tools.stress_devices --count 301

Модель тут не нужна: всё считается на месте.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta

from wam import dialog
from wam.derive import DERIVED_FROM, derive_factors
from wam.diary import Diary
from wam.insights import find_links
from wam.schema import DayRecord, Fact, Timeline
from wam.wearables import NORM_MIN_DAYS, SberRingSource, merge_into

PERMUTATIONS = 300
START = date(2026, 1, 1)


def _ring_day(day: date, **fields) -> dict:
    return {"date": day.isoformat(), **fields}


def _timeline_from_ring(rows: list[dict]) -> Timeline:
    line = Timeline()
    merge_into(line, SberRingSource().read(rows))
    return derive_factors(line)


# ── случаи ────────────────────────────────────────────────────────────────

def bad_sleep_hurts_energy(rng: random.Random, days: int = 60):
    """
    Кольцо видит плохой сон, назавтра человек называет силы низкими.

    Это главный случай, ради которого прибор и нужен: причина пришла с
    устройства, следствие - со слов.
    """
    rows, line = [], Timeline()
    slept = []
    for offset in range(days):
        day = START + timedelta(days=offset)
        bad = rng.random() < 0.5
        slept.append(bad)
        rows.append(_ring_day(day, sleep_score=35 if bad else 85,
                              steps=6000, resting_hr=60))
    merge_into(line, SberRingSource().read(rows))
    for offset in range(days):
        day = START + timedelta(days=offset)
        was_bad = slept[offset - 1] if offset else False
        energy = 6.5 - (2.5 if was_bad else 0.0) + rng.gauss(0, 0.7)
        line.record_for = None            # запись уже есть, ищем её ниже
        for record in line.days:
            if record.day == day:
                record.add(Fact("metric", "энергия",
                                max(0.0, min(10.0, energy)), "diary"))
                break
    derive_factors(line)
    return line, {"вид": "связь по прибору", "фактор": "мало спал",
                  "показатель": "энергия"}


def low_hrv_person(rng: random.Random, days: int = 45):
    """
    У человека вариабельность пульса всегда низкая - это его норма.

    Общая граница объявила бы «организм не восстановился» каждый день. Так
    считает и мир: Oura сравнивает сегодняшнюю вариабельность со скользящим
    средним самого человека, а не с чужой шкалой.
    """
    rows = []
    for offset in range(days):
        day = START + timedelta(days=offset)
        rows.append(_ring_day(day, hrv=26 + rng.gauss(0, 2), sleep_score=70))
    return _timeline_from_ring(rows), {"вид": "своя норма",
                                       "фактор": "организм не восстановился"}


def hrv_drop(rng: random.Random, days: int = 40):
    """У того же человека вариабельность резко падает на несколько дней."""
    rows, dropped = [], set()
    for offset in range(days):
        day = START + timedelta(days=offset)
        drop = offset >= days - 5
        if drop:
            dropped.add(day)
        rows.append(_ring_day(day, hrv=(14 if drop else 26) + rng.gauss(0, 1.5),
                              sleep_score=70))
    return _timeline_from_ring(rows), {"вид": "падение нормы",
                                       "фактор": "организм не восстановился",
                                       "дни": dropped}


def steady_days(rng: random.Random, days: int = 40):
    """Ровные дни: ничего не случилось, и прибор не должен ничего выдумывать."""
    rows = []
    for offset in range(days):
        day = START + timedelta(days=offset)
        rows.append(_ring_day(day, sleep_score=75 + rng.gauss(0, 3),
                              stress_level=35 + rng.gauss(0, 4),
                              steps=8000 + rng.gauss(0, 500),
                              resting_hr=58 + rng.gauss(0, 1.5),
                              hrv=55 + rng.gauss(0, 3)))
    return _timeline_from_ring(rows), {"вид": "ровные дни"}


CASES = [bad_sleep_hurts_energy, low_hrv_person, hrv_drop, steady_days]


# ── проверки ──────────────────────────────────────────────────────────────

def check_case(kind: dict, line: Timeline) -> list[str]:
    claims: list[str] = []
    hot = {name: sum(1 for r in line.days if r.factor(name) == 1.0)
           for name in line.factor_names()}

    if kind["вид"] == "связь по прибору":
        links = find_links(line, permutations=PERMUTATIONS)
        found = [l for l in links
                 if l.factor == kind["фактор"] and l.metric == kind["показатель"]]
        if not found:
            claims.append(f"не нашёл связь «{kind['фактор']}» → «{kind['показатель']}»")
        else:
            link = max(found, key=lambda l: abs(l.effect))
            if link.source != "прибор":
                claims.append(f"связь с прибора помечена как «{link.source}»")
            if link.effect > 0:
                claims.append("плохой сон назвал причиной прилива сил")
        # Тавтологии быть не должно: фактор из показателя против него самого
        for link in links:
            if DERIVED_FROM.get(link.factor) == link.metric and link.lag_days == 0:
                claims.append(f"сравнил «{link.factor}» с «{link.metric}» в тот же день")

    if kind["вид"] == "своя норма":
        # После недели наблюдений обычные для человека дни не должны гореть
        days_after = [r for r in line.days
                      if (r.day - line.days[0].day).days >= NORM_MIN_DAYS]
        burning = [r for r in days_after if r.factor(kind["фактор"]) == 1.0]
        if len(burning) > len(days_after) * 0.2:
            claims.append(f"«{kind['фактор']}» горит {len(burning)} дней "
                          f"из {len(days_after)} на его же норме")

    if kind["вид"] == "падение нормы":
        caught = [r for r in line.days
                  if r.day in kind["дни"] and r.factor(kind["фактор"]) == 1.0]
        if not caught:
            claims.append("падение вариабельности на треть не замечено вовсе")

    if kind["вид"] == "ровные дни":
        loud = {name: times for name, times in hot.items()
                if times > len(line.days) * 0.3}
        if loud:
            claims.append(f"на ровных днях объявил {sorted(loud)}")

    # Общее: показания не должны уезжать в чужой день
    for record in line.days:
        for fact in record.facts:
            if fact.kind == "metric" and fact.source == "sber_ring":
                if not (START <= record.day):
                    claims.append("показание уехало за пределы дневника")
    return claims


def check_talk(rng: random.Random) -> list[str]:
    """Разговор с ползунками: цифры кольца должны попасть в сегодняшний день."""
    claims: list[str] = []
    diary = Diary(f"ring-{rng.randint(0, 10**6)}")
    ring = {"sleep_score": 30, "stress_level": 80, "steps": 1500}

    replies = dialog.step(diary, "пил кофе с утра, к вечеру вымотался",
                          ring=ring, parser=dialog.Parser(), origin="stress")
    today = diary.today()
    if today.metric("качество сна") is None:
        claims.append("показания кольца не попали в сегодняшний день")
    said = "\n".join(m.get("note", "") for m in replies)
    if "с кольца" not in said:
        claims.append("цифры с прибора выданы за слова человека: нет пометки")
    if today.factor("мало спал") != 1.0:
        claims.append("сон 30 из 100 не стал фактором «мало спал»")

    # Рассказ про вчера не должен утаскивать сегодняшние показания во вчера
    other = Diary(f"ring-past-{rng.randint(0, 10**6)}")
    dialog.step(other, "вчера был перелёт", ring=ring, parser=dialog.Parser(),
                origin="stress")
    yesterday = other.record_for(date.today() - timedelta(days=1))
    if yesterday.metric("качество сна") is not None:
        claims.append("сегодняшние показания прибора легли во вчерашний день")
    return claims


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=301,
                        help="сколько проверок всего")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=15)
    args = parser.parse_args()

    counted: dict[str, int] = {}
    examples: list[str] = []
    done = 0
    run = 0
    while done < args.count:
        case = CASES[run % len(CASES)]
        rng = random.Random(args.seed * 1000 + run)
        line, kind = case(rng)
        claims = check_case(kind, line)
        # Каждый пятый прогон - живой разговор с ползунками
        if run % 5 == 0:
            claims += check_talk(rng)
        for claim in claims:
            key = f"[{kind['вид']}] {claim.split(':')[0]}"
            counted[key] = counted.get(key, 0) + 1
            if len(examples) < args.show:
                examples.append(f"{case.__name__} #{run}: {claim}")
        run += 1
        done += 1
        if done % 50 == 0:
            print(f"  прошло {done} из {args.count}", flush=True)

    problems = sum(counted.values())
    print(f"\nПроверок: {done}. Проблемных мест: {problems}.")
    for claim, times in sorted(counted.items(), key=lambda p: -p[1]):
        print(f"  {times:4}  {claim}")
    if examples:
        print("\nПримеры:")
        for line_text in examples:
            print(" ", line_text)
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
