"""
Замер разбора речи: сколько из сказанного понимают правила и сколько модель.

Массовый прогон разговоров (`stress_dialogs`) ищет плохие ответы, но не
отвечает на простой вопрос: зачем вообще модель, если есть словарь? Здесь
ответ числом. Берём фразы с разметкой - что в них на самом деле сказано - и
смотрим, какую долю этого достаёт каждый способ разбора.

Откуда фразы. Большая часть - банк живых реплик из `stress_dialogs`: он
писался для поиска ошибок разговора, а не под этот замер, и подогнать его под
модель или под словарь было бы не с чего. Остальное - фразы из обычных
ситуаций, когда человек пишет дневнику: утро после плохой ночи, голова,
обед, ссора, тренировка. Разметку сделал один разметчик без второго
мнения, поэтому это оценка, а не эталон; спорные места в разметку просто не
включались.

Что считается:

* **пункт разметки** - привычка или событие («кофе», «ссора», «кофе не было»)
  либо показатель самочувствия («сон», и плохой он или хороший);
* **фраза понята целиком** - найдены все её пункты;
* **записано сверх разметки** - то, чего в разметке нет. Это не обязательно
  ошибка (модель вправе записать «работу»), поэтому считается отдельно и
  показывается примерами, а не вычитается.

    python3 -m tools.measure_parsing                         # только правила
    WAM_ENGINE="Claude Code" python3 -m tools.measure_parsing --model

С `--model` замер идёт тремя столбцами: правила, модель сама по себе и связка
«модель плюс правила» - именно так речь разбирается в продукте
(`dialog.Parser`). Модель берётся та же, что у страницы: по ключу в окружении
или через установленный Claude Code. Без модели прогон честно говорит, что
считал только правила.
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from wam import dialog
from wam.extract import RuleExtractor
from wam.schema import DayRecord

# ── разметка ──────────────────────────────────────────────────────────────
#
# Привычка: ("f", шаблон имени, было ли). Шаблон ищется в имени записанного
# факта - правила и модель сводят имена к общим через один словарь
# (`canonical_factor`), но то, чего в словаре нет («курение», «ребёнок не
# спал»), модель называет своими словами, и шаблон это допускает.
#
# Показатель: ("m", какие показатели годятся, направление). Направление «-»
# значит оценка 5 и ниже, «+» - 6 и выше, None - не проверяем. Шкала внутри
# программы одна: больше - лучше, в том числе у тревоги и головной боли.
# Иронию («спал как убитый, целых четыре часа») без направления не поймать.
#
# Где про сон сказаны только часы («спал часов пять»), направление не
# проверяется: перевод часов в балл у программы свой (пять часов - 6 из 10),
# и спорить тут не с разбором, а с договорённостью о шкале.

SLEEP, ENERGY, ANXIETY, MOOD, HEAD = ("качество сна",), ("энергия",), ("тревога",), \
    ("настроение",), ("головная боль",)
ANY_STATE = ("энергия", "настроение", "тревога")


def f(pattern: str, value: float = 1.0) -> tuple:
    return ("f", pattern, value)


def m(names: tuple[str, ...], direction: str | None = None) -> tuple:
    return ("m", names, direction)


# Шаблоны частых привычек. Канонические имена из словаря, плюс то, как их
# называет модель, если пишет своё имя.
COFFEE = r"^кофе"
ALCOHOL = r"^алкоголь"
SPORT = r"^тренировка"
WALK = r"^прогулка"
LATE = r"^поздний отбой"
SCREEN = r"^экран|сериал|телефон|кино|фильм"

# Банк реплик `stress_dialogs` - категории «день», «прошлый день», «тяжёлое»,
# «эмоции», «длинный», «еда», «опечатки», «время», «отрицания», «ирония».
# Фразы без однозначного содержания («ничего особенного», «лёг в 23:40»)
# не размечены и в замер не идут.
FROM_BANK: list[tuple[str, list[tuple]]] = [
    ("пил кофе с утра, потом весь день на ногах", [f(COFFEE)]),
    ("два эспрессо до обеда, вечером зал", [f(COFFEE), f(SPORT)]),
    ("спал часов пять, разбитый весь день", [m(SLEEP), m(ENERGY, "-")]),
    ("с утра пробежка, завтрак нормальный, к вечеру устал",
     [f(SPORT), m(ENERGY, "-")]),
    ("весь день за компом, ни разу не вышел на улицу",
     [f(r"комп|экран|сидяч|без движ|дом|работ|^прогулка$", None)]),
    ("гулял часа полтора, потом ужин и сериал до часу",
     [f(WALK), f(SCREEN), f(LATE)]),
    ("ел мясо, овощи, вечером немного вина", [f(r"^мясо"), f(r"^овощи"), f(ALCOHOL)]),
    ("обед пропустил, вечером наелся до отвала",
     [f(r"^пропустил обед"), f(r"^переедание")]),
    ("тренировка утром, потом работа, лёг рано", [f(SPORT)]),
    ("работал из дома, кофе литрами, ложился в два",
     [f(COFFEE), f(LATE), f(r"дом|удал")]),
    ("днём выпил энергетик, вечером не мог уснуть", [f(COFFEE), m(SLEEP, "-")]),
    ("было совещание на три часа, голова к вечеру гудела",
     [f(r"^много встреч|совещ"), m(HEAD, "-")]),
    ("перелёт в москву, спал в самолёте часа два", [f(r"^перелёт"), m(SLEEP, "-")]),
    ("с утра йога, днём салат, вечером прогулка вдоль реки",
     [f(SPORT), f(r"^овощи"), f(WALK)]),
    ("пиво с друзьями, посидели допоздна",
     [f(ALCOHOL), f(r"друз"), f(LATE)]),
    ("фастфуд на обед, потом тяжесть в животе", [f(r"^фастфуд")]),
    ("весь день дедлайн, курил больше обычного", [f(r"^аврал"), f(r"кур|сигарет")]),
    ("вчера пил вино и лёг за полночь", [f(ALCOHOL), f(LATE)]),
    ("позавчера была тренировка, спал отлично", [f(SPORT), m(SLEEP, "+")]),
    ("в понедельник был на даче, много гулял", [f(WALK), f(r"дач|загород|природ")]),
    ("в среду работал из дома, кофе литрами", [f(COFFEE), f(r"дом|удал")]),
    ("в четверг заболел, температура, весь день лежал", [f(r"^болезнь")]),
    ("у меня умер дедушка, весь день на похоронах", [f(r"^тяжёлое событие")]),
    ("меня уволили сегодня", [f(r"^тяжёлое событие|увольн")]),
    ("мы развелись, я живу один", [f(r"^тяжёлое событие|развод")]),
    ("весь день в больнице с мамой", [f(r"^тяжёлое событие|больниц")]),
    ("попал в аварию, машина всмятку", [f(r"^тяжёлое событие|авари")]),
    ("расстались с девушкой, полночи не спал",
     [f(r"^тяжёлое событие|расстав"), m(SLEEP, "-")]),
    ("блин ну и денёк, всё бесит", [m(MOOD, "-")]),
    ("на работе полный трэш, начальник орал при всех",
     [f(r"^ссора|конфликт|начальн|руковод|^аврал|работ")]),
    ("поругался с женой вечером, потом полночи не спал",
     [f(r"^ссора"), m(SLEEP, "-")]),
    ("тревожно весь день, непонятно почему", [m(ANXIETY, "-")]),
    ("настроение отличное, всё получается", [m(MOOD, "+")]),
    ("ну что сказать, день был такой: встал в 6, две чашки кофе до обеда, "
     "потом созвон на два часа с клиентом, обед пропустил вообще, вечером "
     "зал, после зала бургер и кола, лёг в час ночи, спал плохо, голова к "
     "вечеру начала болеть",
     [f(COFFEE), f(r"^много встреч"), f(r"^пропустил обед"), f(SPORT),
      f(r"^фастфуд"), f(LATE), m(SLEEP, "-"), m(HEAD, "-")]),
    ("с утра всё шло нормально, завтрак, кофе, работа, но потом позвонили "
     "из школы, пришлось ехать за ребёнком, обед пропустил, вечером пытался "
     "нагнать работу, лёг в два, сплю плохо третью ночь подряд",
     [f(COFFEE), f(r"школ|ребён|ребен|дет|семь"), f(r"^пропустил обед"),
      f(LATE), m(SLEEP, "-")]),
    ("суббота была спокойная: выспался, долго завтракали, гуляли часа три "
     "по парку, потом кино, вечером немного вина, лёг в одиннадцать, "
     "чувствую себя человеком впервые за неделю",
     [f(WALK), f(SCREEN), f(ALCOHOL), m(SLEEP, "+")]),
    ("на завтрак овсянка, обед пропустил, ужин поздно",
     [f(r"^пропустил обед"), f(r"^поздний ужин")]),
    ("весь день на фастфуде", [f(r"^фастфуд")]),
    ("ел рыбу и овощи, сладкого не было",
     [f(r"^рыба"), f(r"^овощи"), f(r"^сладкое", 0.0)]),
    ("объелся на ночь, потом тяжело было уснуть",
     [f(r"^переедание|^поздний ужин"), m(SLEEP, "-")]),
    ("пил кофе с утар, спал часво пять", [f(COFFEE), m(SLEEP)]),
    ("трениовка была вечером, устал", [f(SPORT), m(ENERGY, "-")]),
    ("гулял час, потмо ужин", [f(WALK)]),
    ("спал плхо, голова болит", [m(SLEEP, "-"), m(HEAD, "-")]),
    ("последний кофе был в 17", [f(COFFEE)]),
    ("тренировка в 7 утра", [f(SPORT)]),
    ("ужинал в 22:30", [f(r"^поздний ужин")]),
    ("сегодня без кофе совсем", [f(COFFEE, 0.0)]),
    ("не тренировался, не гулял", [f(SPORT, 0.0), f(WALK, 0.0)]),
    ("алкоголя не было", [f(ALCOHOL, 0.0)]),
    ("сладкого не ел", [f(r"^сладкое", 0.0)]),
    ("экран перед сном не трогал", [f(r"^экран", 0.0)]),
    ("ну конечно, спал как убитый, целых четыре часа", [m(SLEEP, "-")]),
    ("прекрасный день, всё сломалось", [m(MOOD, "-")]),
]

# Фразы из ситуаций, в которых человек пишет дневнику (раздел «Когда открывают
# Миру» в README). Написаны для этого замера, разметка та же.
SITUATIONS: list[tuple[str, list[tuple]]] = [
    ("проснулся разбитый, спал часов пять", [m(SLEEP), m(ENERGY, "-")]),
    ("с утра башка трещит", [m(HEAD, "-")]),
    ("голова раскалывается с обеда, выпил таблетку",
     [m(HEAD, "-"), f(r"таблет|обезбол|лекарств")]),
    ("после обеда в сон клонит, съел две тарелки плова",
     [m(ENERGY, "-"), f(r"^переедание|плов|^мясо")]),
    ("третья чашка кофе за день, руки трясутся", [f(COFFEE)]),
    ("вчера на корпоративе выпил лишнего, сегодня никакой",
     [f(ALCOHOL), m(ENERGY + MOOD, "-")]),
    ("после созвона с заказчиком весь на нервах",
     [f(r"^много встреч|созвон|заказчик"), m(ANXIETY, "-")]),
    ("поцапались с мужем из-за денег, весь вечер ревела",
     [f(r"^ссора"), m(MOOD, "-")]),
    ("сходила на пилатес, отпустило", [f(SPORT), m(ANY_STATE, "+")]),
    ("вечером полтора часа тупил в телефон в кровати", [f(r"^экран|телефон")]),
    ("уснул только в три, всё думал о завтрашнем отчёте",
     [m(SLEEP, "-"), m(ANXIETY, "-"), f(r"отчёт|работ|^аврал|дедлайн")]),
    ("весь день на ногах по жаре, к вечеру еле живой",
     [m(ENERGY, "-"), f(r"жар|зной|погод")]),
    ("сегодня в спортзал не пошёл, лень", [f(SPORT, 0.0)]),
    ("пообедал шаурмой на бегу", [f(r"^фастфуд")]),
    ("выпила бокал вина за ужином, уснула быстро, но проснулась в 4",
     [f(ALCOHOL), m(SLEEP, "-")]),
    ("мама снова звонила с претензиями, полдня трясло",
     [f(r"^ссора|конфликт|мам|семь|претенз|звон"), m(ANXIETY + MOOD, "-")]),
    ("ходил к стоматологу, полдня отходил от наркоза",
     [f(r"стоматол|врач|зуб|наркоз|лечени|процедур"), m(ENERGY, "-")]),
    ("в метро давка, опоздал, начальник отчитал",
     [f(r"начальн|руковод|^ссора|конфликт|опозд|выговор")]),
    ("сегодня отлично выспалась, бодрая с утра", [m(SLEEP, "+"), m(ENERGY, "+")]),
    ("в обед полчаса прошёлся пешком вокруг офиса", [f(WALK)]),
    ("ночью ребёнок просыпался три раза, я никакая",
     [f(r"ребён|ребен|дет|просып|пробужд"), m(SLEEP + ENERGY, "-")]),
    ("выпил после шести двойной капучино, теперь не уснуть",
     [f(COFFEE), m(SLEEP, "-")]),
]

PHRASES = FROM_BANK + SITUATIONS


# ── сверка ────────────────────────────────────────────────────────────────

@dataclass
class Score:
    """Итог одного способа разбора на всех фразах."""

    items: int = 0
    found: int = 0
    phrases: int = 0
    whole: int = 0
    empty: int = 0          # не записано вообще ничего
    errors: int = 0         # модель не ответила
    extra: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"пунктов найдено {self.found} из {self.items} "
                f"({_share(self.found, self.items)}), "
                f"фраз понято целиком {self.whole} из {self.phrases} "
                f"({_share(self.whole, self.phrases)}), "
                f"пустой разбор {self.empty}, "
                f"записано сверх разметки {len(self.extra)}"
                + (f", модель не ответила {self.errors}" if self.errors else ""))


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def _item_found(item: tuple, record: DayRecord) -> bool:
    kind, what, expected = item
    if kind == "f":
        # None - годится и «было», и «не было»: «ни разу не вышел на улицу»
        # можно честно записать и как «прогулки не было», и как «сидел дома».
        return any(fact.kind == "factor" and re.search(what, fact.name)
                   and (expected is None or fact.value == expected)
                   for fact in record.facts)
    for fact in record.facts:
        if fact.kind != "metric" or fact.name not in what:
            continue
        if expected is None:
            return True
        if expected == "-" and fact.value <= 5:
            return True
        if expected == "+" and fact.value >= 6:
            return True
    return False


def _extra(items: list[tuple], record: DayRecord) -> list[str]:
    """Что записано, а в разметке не значится."""
    extra = []
    for fact in record.facts:
        if fact.kind == "factor":
            # «кофе утром» идёт вслед за «кофе» и сам по себе не лишний
            names = (fact.name, fact.name.rsplit(" ", 1)[0])
            if any(k == "f" and any(re.search(p, n) for n in names)
                   for k, p, _ in items):
                continue
        elif fact.kind == "metric":
            if any(k == "m" and fact.name in names_ for k, names_, _ in items):
                continue
        else:
            continue                       # события в замер не входят
        extra.append(f"{fact.name}={fact.value:g}")
    return extra


def score(records: list[DayRecord | None],
          phrases: list[tuple[str, list[tuple]]] | None = None) -> Score:
    """Сверить разборы с разметкой. None - модель не ответила."""
    total = Score()
    for (text, items), record in zip(phrases or PHRASES, records):
        total.phrases += 1
        total.items += len(items)
        if record is None:
            total.errors += 1
            total.empty += 1
            total.missed.append(f"«{text[:50]}»: модель не ответила")
            continue
        got = [item for item in items if _item_found(item, record)]
        total.found += len(got)
        if len(got) == len(items):
            total.whole += 1
        else:
            lost = [item[1] if item[0] == "f" else "/".join(item[1])
                    for item in items if item not in got]
            total.missed.append(f"«{text[:50]}»: не найдено {lost}")
        if not any(f.kind in ("factor", "metric") for f in record.facts):
            total.empty += 1
        total.extra += [f"«{text[:40]}»: {x}" for x in _extra(items, record)]
    return total


def by_rules() -> list[DayRecord]:
    rules = RuleExtractor()
    return [rules.extract(text, date.today()) for text, _ in PHRASES]


def by_model(workers: int) -> tuple[str, list[DayRecord | None], list[DayRecord]]:
    """
    Модель сама по себе и связка «модель плюс правила», как в продукте.

    Ответ модели кешируется: связке нужен тот же самый ответ, иначе сравнение
    шло бы между двумя разными бросками одной и той же модели.
    """
    parser = dialog.Parser.from_environment()
    if parser.model is None:
        raise SystemExit("Модели нет: задайте ключ или WAM_ENGINE (см. README).")
    raw = parser.model._complete
    cache: dict[str, str] = {}

    def remembered(prompt: str) -> str:
        if prompt not in cache:
            cache[prompt] = raw(prompt)
        return cache[prompt]

    parser.model._complete = remembered

    def one(text: str) -> tuple[DayRecord | None, DayRecord]:
        try:
            alone = parser.model.extract(text, date.today())
        except Exception:
            alone = None
        return alone, parser.parse(text, date.today())

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, [text for text, _ in PHRASES]))
    return (parser.engine, [a for a, _ in results], [b for _, b in results])


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--model", action="store_true",
                      help="разобрать фразы ещё и моделью")
    args.add_argument("--workers", type=int, default=6)
    args.add_argument("--show", type=int, default=15,
                      help="сколько промахов и лишних записей показать")
    options = args.parse_args()

    print(f"Фраз: {len(PHRASES)} (из банка реплик {len(FROM_BANK)}, "
          f"из ситуаций {len(SITUATIONS)}), "
          f"пунктов разметки: {sum(len(i) for _, i in PHRASES)}.\n")
    columns = [("Правила", by_rules())]
    if options.model:
        engine, alone, both = by_model(options.workers)
        columns += [(f"Модель ({engine})", alone),
                    (f"Модель + правила, как в продукте", both)]
    else:
        print("Модель не запускалась: без --model считаются только правила.\n")

    # Банк реплик и словарь правил росли вместе: на этих фразах словарь
    # чинили, и правила на них заведомо сильнее, чем на новой речи. Поэтому
    # кроме общего итога - итог по каждой части отдельно.
    bank = len(FROM_BANK)
    for name, records in columns:
        print(f"{name}: {score(records).line()}")
        print(f"   банк реплик: {score(records[:bank], FROM_BANK).line()}")
        print(f"   ситуации:    {score(records[bank:], SITUATIONS).line()}")
    for name, records in columns:
        result = score(records)
        print(f"\n{name} - промахи:")
        for line in result.missed[:options.show]:
            print("  ", line)
        if len(result.missed) > options.show:
            print(f"   ... и ещё {len(result.missed) - options.show}")
        print(f"{name} - сверх разметки:")
        for line in result.extra[:options.show]:
            print("  ", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
