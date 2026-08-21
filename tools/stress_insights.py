"""
Проверка выводов: правильно ли дневник считает, а не только разбирает речь.

Разговор можно разобрать безупречно и при этом сказать человеку глупость.
Здесь проверяется вторая половина: движок связей. Дневники собираются
искусственно, с заранее известной правдой, и прогон смотрит, совпал ли ответ
программы с тем, что в данные заложено.

Что проверяется:

- **настоящая связь находится** - если кофе правда портит сон, это должно быть
  видно, и в нужную сторону;
- **пустышка не выдаётся за вывод** - на дневнике из чистого шума
  «подтверждённых» связей быть почти не должно; допускается ровно та доля,
  которую обещает порог значимости;
- **третий фактор распознаётся** - когда кофе и плохой сон случаются вместе
  из-за аврала, программа обязана сказать, что дело не в кофе;
- **малые данные называются наблюдением**, а не выводом;
- **слова не противоречат числам** - фраза о связи says/basis не должна
  говорить «лучше», когда эффект отрицательный.

    python3 -m tools.stress_insights --count 60 --seed 1

Модель тут не нужна: всё считается на месте. Прогон ничего не пишет на диск.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta

from wam import phrases
from wam.insights import find_links
from wam.schema import DayRecord, Fact, Timeline

# Перемешиваний хватает и меньше, чем в продукте: здесь проверяются пороги
# поведения, а не третий знак p-значения. Прогон при этом идёт секунды.
PERMUTATIONS = 300

START = date(2026, 1, 1)


def _timeline(days: int) -> Timeline:
    line = Timeline()
    for offset in range(days):
        line.add(DayRecord(day=START + timedelta(days=offset)))
    return line


def _put(line: Timeline, index: int, kind: str, name: str, value: float) -> None:
    line.days[index].add(Fact(kind, name, value, "diary"))


def real_link(rng: random.Random, days: int = 60, effect: float = -2.0,
              lag: int = 1) -> tuple[Timeline, dict]:
    """Кофе правда портит сон на следующий день. Шум есть, но связь сильная."""
    line = _timeline(days)
    for i in range(days):
        drank = rng.random() < 0.5
        _put(line, i, "factor", "кофе", 1.0 if drank else 0.0)
        base = 6.5 + rng.gauss(0, 0.8)
        if drank and i + lag < days:
            _put(line, i + lag, "metric", "качество сна",
                 max(0.0, min(10.0, base + effect)))
    for i in range(days):
        if line.days[i].metric("качество сна") is None:
            _put(line, i, "metric", "качество сна",
                 max(0.0, min(10.0, 6.5 + rng.gauss(0, 0.8))))
    return line, {"вид": "настоящая связь", "фактор": "кофе",
                  "показатель": "качество сна", "сторона": "хуже"}


def pure_noise(rng: random.Random, days: int = 60,
               factors: int = 4) -> tuple[Timeline, dict]:
    """Никаких связей: всё случайно. Программа обязана молчать."""
    line = _timeline(days)
    names = ["кофе", "тренировка", "сладкое", "прогулка"][:factors]
    for i in range(days):
        for name in names:
            _put(line, i, "factor", name, 1.0 if rng.random() < 0.5 else 0.0)
        _put(line, i, "metric", "энергия",
             max(0.0, min(10.0, 6.0 + rng.gauss(0, 1.2))))
    return line, {"вид": "пустышка"}


def confounded(rng: random.Random, days: int = 70) -> tuple[Timeline, dict]:
    """
    Аврал заставляет и пить кофе, и плохо спать. Связи «кофе - сон» нет:
    в дни без аврала кофе на сон не влияет вовсе.
    """
    line = _timeline(days)
    for i in range(days):
        rush = rng.random() < 0.45
        _put(line, i, "factor", "аврал", 1.0 if rush else 0.0)
        drank = rng.random() < (0.85 if rush else 0.2)
        _put(line, i, "factor", "кофе", 1.0 if drank else 0.0)
        sleep = 7.0 + rng.gauss(0, 0.7) - (2.2 if rush else 0.0)
        _put(line, i, "metric", "качество сна", max(0.0, min(10.0, sleep)))
    return line, {"вид": "третий фактор", "фактор": "кофе",
                  "виноват": "аврал", "показатель": "качество сна"}


def small_data(rng: random.Random, days: int = 12) -> tuple[Timeline, dict]:
    """Данных мало: что бы ни нашлось, это наблюдение, а не вывод."""
    line = _timeline(days)
    for i in range(days):
        drank = rng.random() < 0.5
        _put(line, i, "factor", "кофе", 1.0 if drank else 0.0)
        _put(line, i, "metric", "энергия",
             max(0.0, min(10.0, 6.0 - (2.0 if drank else 0.0) + rng.gauss(0, 0.6))))
    return line, {"вид": "мало данных"}


def helpful_link(rng: random.Random, days: int = 60) -> tuple[Timeline, dict]:
    """Тренировка добавляет сил - связь в хорошую сторону, её тоже надо видеть."""
    line = _timeline(days)
    for i in range(days):
        trained = rng.random() < 0.5
        _put(line, i, "factor", "тренировка", 1.0 if trained else 0.0)
        _put(line, i, "metric", "энергия",
             max(0.0, min(10.0, 5.5 + (2.0 if trained else 0.0) + rng.gauss(0, 0.8))))
    return line, {"вид": "настоящая связь", "фактор": "тренировка",
                  "показатель": "энергия", "сторона": "лучше"}


CASES = [real_link, pure_noise, confounded, small_data, helpful_link]


def _strong(links: list) -> list:
    return [l for l in links if l.strength != "наблюдение" and not l.confounder]


def check(kind: dict, links: list) -> list[str]:
    """Что не сошлось между заложенной правдой и ответом программы."""
    claims: list[str] = []
    strong = _strong(links)

    if kind["вид"] == "настоящая связь":
        found = [l for l in strong
                 if l.factor == kind["фактор"] and l.metric == kind["показатель"]]
        if not found:
            claims.append(f"не нашёл связь «{kind['фактор']}» → «{kind['показатель']}»")
        else:
            link = max(found, key=lambda l: abs(l.effect))
            if link.direction != kind["сторона"]:
                claims.append(f"сторона связи наоборот: {link.direction} "
                              f"вместо {kind['сторона']}")
            said = phrases.say(link)
            if kind["сторона"] == "хуже" and "лучше" in said.split(",")[0]:
                claims.append(f"числа говорят «хуже», слова - «лучше»: {said}")

    if kind["вид"] == "пустышка" and strong:
        names = ", ".join(f"{l.factor}→{l.metric}" for l in strong[:3])
        claims.append(f"на чистом шуме выдал вывод: {names}")

    if kind["вид"] == "третий фактор":
        wrong = [l for l in strong
                 if l.factor == kind["фактор"] and l.metric == kind["показатель"]]
        if wrong:
            claims.append("объявил связь «кофе → сон», хотя виноват аврал")
        blamed = [l for l in links if l.factor == kind["фактор"] and l.confounder]
        real = [l for l in strong if l.factor == kind["виноват"]]
        if not blamed and not real:
            claims.append("не заметил ни третьего фактора, ни настоящей причины")

    if kind["вид"] == "мало данных" and strong:
        claims.append("на двенадцати днях выдал вывод вместо наблюдения")

    # Общие проверки, они же на всех дневниках сразу
    for link in links:
        if link.days_with < 2 or link.days_without < 2:
            claims.append(f"вывод по {link.days_with}/{link.days_without} дням")
        if link.effect > 0 and link.direction != "лучше":
            claims.append("направление не совпадает со знаком эффекта")
        if not phrases.basis(link).strip():
            claims.append("вывод без объяснения, откуда он взялся")
    return claims


# Сколько промахов допустимо. Ноль тут недостижим и обещать его - вранье:
# порог значимости в 5% ровно это и означает - примерно одна связь из двадцати
# на чистом шуме проходит проверку. Мы говорим об этом человеку прямо, и
# прогон меряет, держится ли обещанная доля.
ALLOWED = {
    "настоящая связь": 0.05,   # изредка сильный шум прячет настоящую связь
    "пустышка": 0.05,          # столько же ложных даёт сам порог значимости
    "третий фактор": 0.05,
    "мало данных": 0.0,        # тут ошибаться нельзя вовсе: правило жёсткое
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=60,
                        help="сколько дневников на каждый вид")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--show", type=int, default=20)
    args = parser.parse_args()

    counted: dict[str, int] = {}
    examples: list[str] = []
    by_case: dict[str, list[int]] = {}
    total = 0

    for case in CASES:
        for run in range(args.count):
            rng = random.Random(args.seed * 1000 + run)
            line, kind = case(rng)
            links = find_links(line, seed=run, permutations=PERMUTATIONS)
            total += 1
            claims = check(kind, links)
            seen = by_case.setdefault(kind["вид"], [0, 0])
            seen[0] += 1
            if claims:
                seen[1] += 1
            for claim in claims:
                key = f"[{kind['вид']}] {claim.split(':')[0]}"
                counted[key] = counted.get(key, 0) + 1
                if len(examples) < args.show:
                    examples.append(f"{case.__name__} #{run}: {claim}")

    print(f"Дневников: {total}.")
    over = []
    for kind, (runs, bad) in sorted(by_case.items()):
        share = bad / runs
        limit = ALLOWED.get(kind, 0.0)
        mark = "" if share <= limit else "  ← выше допустимого"
        if share > limit:
            over.append(kind)
        print(f"  {kind}: {bad} из {runs} ({share:.0%}, допустимо {limit:.0%}){mark}")
    for claim, times in sorted(counted.items(), key=lambda p: -p[1]):
        print(f"  {times:4}  {claim}")
    if examples:
        print("\nПримеры:")
        for line in examples:
            print(" ", line)
    return 0 if not over else 1


if __name__ == "__main__":
    sys.exit(main())
