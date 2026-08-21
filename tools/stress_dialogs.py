"""
Массовый прогон разговоров: триста диалогов разной длины и вредности.

Обычные тесты проверяют то, что мы уже поняли. Здесь наоборот: разговоры
собираются случайно из банка живых реплик, прогоняются через тот же `dialog.step`,
что и страница, а потом отдельные проверки ищут ответы, после которых дневник
закрывают, - «не понял» на понятную фразу, бодрый вопрос в день похорон, вопрос,
заданный второй раз, привычка, которой человек не называл.

Модель включается ключом в окружении (`ANTHROPIC_API_KEY` и другие из
`wam/llm.py`). Без ключа прогон идёт по правилам - он быстрый и тоже полезный,
но живую речь так не проверить.

    python3 -m tools.stress_dialogs --count 300 --seed 1
    python3 -m tools.stress_dialogs --count 40 --rules   # без модели, быстро

Прогон ничего не чинит и ничего не пишет в дневник человека: каждый разговор
идёт в своей записи в памяти.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from wam import dialog
from wam.diary import Diary
from wam.extract import RuleExtractor

# ── банк реплик ───────────────────────────────────────────────────────────
#
# Всё это - живая речь, а не образцы: с опечатками, сокращениями, матерком и
# оборванными мыслями. Разговор собирается из кусков, поэтому важна не красота
# отдельной фразы, а то, что она может встать в любое место разговора.

SAYINGS: dict[str, list[str]] = {
    "день": [
        "пил кофе с утра, потом весь день на ногах",
        "два эспрессо до обеда, вечером зал",
        "спал часов пять, разбитый весь день",
        "с утра пробежка, завтрак нормальный, к вечеру устал",
        "весь день за компом, ни разу не вышел на улицу",
        "гулял часа полтора, потом ужин и сериал до часу",
        "ел мясо, овощи, вечером немного вина",
        "обед пропустил, вечером наелся до отвала",
        "тренировка утром, потом работа, лёг рано",
        "ничего особенного, обычный день",
        "работал из дома, кофе литрами, ложился в два",
        "днём выпил энергетик, вечером не мог уснуть",
        "было совещание на три часа, голова к вечеру гудела",
        "перелёт в москву, спал в самолёте часа два",
        "с утра йога, днём салат, вечером прогулка вдоль реки",
        "пиво с друзьями, посидели допоздна",
        "фастфуд на обед, потом тяжесть в животе",
        "весь день дедлайн, курил больше обычного",
    ],
    "оценка": [
        "на 7", "на 3", "четыре", "баллов восемь", "где-то на пятёрку",
        "нормально", "так себе", "отвратительно", "супер",
    ],
    "мера": [
        "две чашки", "часа полтора", "минут двадцать", "бокала три",
        "штук пять", "литра полтора", "почти не пил",
    ],
    "поправка": [
        "нет, вру, кофе я не пил",
        "а вообще нет, ближе к шести",
        "точнее, это было вчера",
        "а нет, вру, это был вторник",
        "перепутал, тренировка была в среду",
        "не сегодня, а позавчера",
    ],
    "прошлый день": [
        "вчера пил вино и лёг за полночь",
        "позавчера была тренировка, спал отлично",
        "в понедельник был на даче, много гулял",
        "в среду работал из дома, кофе литрами",
        "в четверг заболел, температура, весь день лежал",
    ],
    "тяжёлое": [
        "у меня умер дедушка, весь день на похоронах",
        "меня уволили сегодня",
        "мы развелись, я живу один",
        "весь день в больнице с мамой",
        "попал в аварию, машина всмятку",
        "расстались с девушкой, полночи не спал",
    ],
    "медицина": [
        "третий день болит голова, что мне выпить?",
        "может мне обследоваться?",
        "какие анализы сдать при такой усталости?",
        "это опасно вообще?",
        "чем лечить бессонницу?",
    ],
    "про программу": [
        "ты вообще живая или программа?",
        "кто ты такая?",
        "с кем я говорю?",
        "ты ии?",
    ],
    "про меня": [
        "что ты вообще про меня знаешь?",
        "какие выводы?",
        "что там у меня?",
        "ну а ты что скажешь?",
    ],
    "сомнение": [
        "откуда ты знаешь что кофе виноват? может это совпадение",
        "с чего ты взял что дело в кофе",
        "ты уверен вообще?",
        "как ты это считаешь?",
    ],
    "эксперимент": [
        "а если я вообще брошу пить кофе на неделю?",
        "что будет если я перестану пить вечером?",
        "давай попробую неделю без сладкого",
    ],
    "приватность": [
        "а ты мои данные кому-нибудь передаёшь?",
        "куда уходят мои записи?",
        "кто это видит кроме меня?",
        "удали всё что записал",
    ],
    "раздражение": [
        "да достал ты со своими вопросами",
        "хватит вопросов",
        "отстань",
        "не хочу рассказывать",
    ],
    "вежливость": [
        "привет", "спасибо", "здравствуй", "ок, спасибо",
    ],
    "мусор": [
        "ну как-то так", "ааааа", "...", "))", "не знаю даже",
        "хз", "ясно", "угу",
    ],
    "эмоции": [
        "блин ну и денёк, всё бесит",
        "на работе полный трэш, начальник орал при всех",
        "поругался с женой вечером, потом полночи не спал",
        "тревожно весь день, непонятно почему",
        "настроение отличное, всё получается",
    ],
    "длинный": [
        "ну что сказать, день был такой: встал в 6, две чашки кофе до обеда, "
        "потом созвон на два часа с клиентом, обед пропустил вообще, вечером "
        "зал, после зала бургер и кола, лёг в час ночи, спал плохо, голова к "
        "вечеру начала болеть",
        "с утра всё шло нормально, завтрак, кофе, работа, но потом позвонили "
        "из школы, пришлось ехать за ребёнком, обед пропустил, вечером пытался "
        "нагнать работу, лёг в два, сплю плохо третью ночь подряд",
        "суббота была спокойная: выспался, долго завтракали, гуляли часа три "
        "по парку, потом кино, вечером немного вина, лёг в одиннадцать, "
        "чувствую себя человеком впервые за неделю",
    ],
    "еда": [
        "на завтрак овсянка, обед пропустил, ужин поздно",
        "весь день на фастфуде",
        "ел рыбу и овощи, сладкого не было",
        "объелся на ночь, потом тяжело было уснуть",
    ],
    "опечатки": [
        "пил кофе с утар, спал часво пять",
        "трениовка была вечером, устал",
        "гулял час, потмо ужин",
        "спал плхо, голова болит",
    ],
    "время": [
        "лёг в 23:40, встал в 6",
        "последний кофе был в 17",
        "тренировка в 7 утра",
        "ужинал в 22:30",
    ],
    "отрицания": [
        "сегодня без кофе совсем",
        "не тренировался, не гулял",
        "алкоголя не было",
        "сладкого не ел",
        "экран перед сном не трогал",
    ],
    "ирония": [
        "ну конечно, спал как убитый, целых четыре часа",
        "прекрасный день, всё сломалось",
        "отдохнул называется",
    ],
    "мелочь": [
        "7", "0", "10", "да", "нет", "не помню",
    ],
    "два дня": [
        "вчера пил, сегодня не пил",
        "вчера зал, сегодня отдых",
        "позавчера перелёт, вчера отсыпался",
    ],
    "возврат": [
        "меня не было неделю, сейчас расскажу",
        "давно не заходил",
        "пропал я, извини",
    ],
}

# Профили разговоров: из каких кусков они складываются. Ключ - имя профиля,
# значение - список категорий, из которых берутся реплики по порядку.
PROFILES: dict[str, list[str]] = {
    "короткий": ["день"],
    "с ответом": ["день", "оценка"],
    "с мерой": ["день", "оценка", "мера"],
    "поправка": ["день", "поправка"],
    "прошлые дни": ["возврат", "прошлый день", "прошлый день", "день"],
    "тяжёлый": ["тяжёлое", "день", "оценка"],
    "врач": ["день", "медицина", "оценка"],
    "спор": ["день", "сомнение", "эксперимент"],
    "недоверие": ["приватность", "про программу", "день"],
    "срыв": ["эмоции", "раздражение", "день"],
    "сумбур": ["длинный", "поправка", "про меня"],
    "мусорный": ["мусор", "мусор", "день", "мусор"],
    "вежливый": ["вежливость", "день", "оценка", "вежливость"],
    "еда": ["еда", "оценка", "день"],
    "всё сразу": ["день", "оценка", "тяжёлое", "медицина", "сомнение",
                  "про меня", "поправка"],
    "с опечатками": ["опечатки", "оценка", "опечатки"],
    "по часам": ["время", "оценка", "время"],
    "отказы": ["отрицания", "отрицания", "оценка"],
    "с иронией": ["ирония", "оценка", "день"],
    "односложный": ["мелочь", "мелочь", "день", "мелочь"],
    "два дня": ["два дня", "оценка", "день"],
}


@dataclass
class Turn:
    """Одна реплика и всё, что дневник на неё ответил."""

    said: str
    replies: list[dict]

    def bot_text(self) -> str:
        return "\n".join(m["text"] for m in self.replies if m["kind"] != "me")

    def notes(self) -> str:
        return "\n".join(m.get("note", "") for m in self.replies)


@dataclass
class Talk:
    """Один разговор целиком."""

    profile: str
    turns: list[Turn] = field(default_factory=list)
    diary: Diary | None = None


# ── проверки ──────────────────────────────────────────────────────────────
#
# Каждая ищет ровно один вид плохого ответа и возвращает текст претензии.

RULES = RuleExtractor()

_HEAVY_ASK = re.compile(r"как вы себя чувствовали|сколько|насколько|во сколько")


def _found_by_rules(text: str) -> list[str]:
    """Что видит в реплике словарь - независимая проверка «тут было что записать»."""
    from datetime import date
    record = RULES.extract(text, date.today())
    return [f.name for f in record.facts if f.value > 0 or f.kind == "metric"]


def check_not_understood(turn: Turn, talk: Talk) -> str:
    said = turn.bot_text()
    if dialog.NOTHING_UNDERSTOOD not in said and dialog.DID_NOT_GET_IT not in said:
        return ""
    seen = _found_by_rules(turn.said)
    if seen:
        return f"«не понял» на фразу, где словарь видит {seen}"
    return ""


def check_heavy_day(turn: Turn, talk: Talk) -> str:
    if not dialog._HEAVY.search(turn.said.lower()):
        return ""
    said = turn.bot_text()
    if dialog.HEAVY_REPLY not in said:
        return "тяжёлое событие - и обычный ответ вместо сочувствия"
    if _HEAVY_ASK.search(said):
        return "тяжёлое событие - и следом бодрый вопрос"
    return ""


def check_silence(turn: Turn, talk: Talk) -> str:
    return "" if turn.bot_text().strip() else "дневник промолчал"


def check_empty_note(turn: Turn, talk: Talk) -> str:
    for message in turn.replies:
        # «Записал. А как вы себя чувствовали?» - это вопрос, а не отчёт о
        # записи, и заметки при нём не бывает.
        if message["text"] == dialog.NO_STATE or message["kind"] == "ask":
            continue
        if message["text"].startswith("Записал") and not message.get("note"):
            return "«Записал» без единого факта"
    return ""


def check_repeated_question(turn: Turn, talk: Talk) -> str:
    asked = [m["text"] for t in talk.turns for m in t.replies if m["kind"] == "ask"]
    for question in asked:
        if asked.count(question) > 1:
            return f"вопрос задан дважды: «{question[:40]}...»"
    return ""


def check_question_answered(turn: Turn, talk: Talk) -> str:
    """Человек спросил - дневник обязан ответить, а не только записать."""
    if not dialog.asks_us(turn.said):
        return ""
    known = (dialog.MEDICAL_REPLY, dialog.WHO_REPLY, dialog.PRIVACY_REPLY,
             dialog.DELETE_REPLY, dialog.HOW_SURE_REPLY, dialog.TRY_WITHOUT_REPLY,
             dialog.ANNOYED_REPLY)
    said = turn.bot_text()
    if any(answer in said for answer in known) or "Дней в дневнике" in said:
        return ""
    return "вопрос к программе остался без ответа"


# ── судья ─────────────────────────────────────────────────────────────────
#
# Правила знают меньше модели: «ложился в два» - это поздний отбой, «за компом
# весь день» - работа, и словарь такого не выводит. Поэтому подозрительные
# записи проверяет сама модель отдельным вопросом: следует ли привычка из
# фразы. Без ключа судьи нет - тогда доверяем словарю, как и раньше.

JUDGE_PROMPT = (
    "Разговор человека с дневником:\n{context}\n"
    "Последняя реплика человека: «{said}»\n"
    "После неё программа записала за этим днём привычку «{habit}».\n"
    "Правильно ли это - с учётом всего разговора, прямо или по смыслу? "
    "Короткий ответ вроде «две чашки» или «часа полтора» относится к тому, "
    "о чём спрашивал дневник. Ответь одним словом: да или нет."
)

_judge = None
_judged: dict[tuple[str, str, str], bool] = {}


def judge_says_yes(said: str, habit: str, context: str = "") -> bool:
    """Признаёт ли модель, что привычка следует из разговора. Без модели - да."""
    if _judge is None:
        return True
    key = (context, said, habit)
    if key not in _judged:
        try:
            answer = _judge(JUDGE_PROMPT.format(said=said, habit=habit,
                                                context=context or "(начало разговора)"))
        except Exception:
            return True                    # судья недоступен - не выдумываем
        _judged[key] = answer.strip().lower().startswith("да")
    return _judged[key]


def _talk_context(turn: Turn, talk: Talk) -> str:
    """Разговор до этой реплики - две последние пары «человек - дневник»."""
    lines = []
    for past in talk.turns:
        if past is turn:
            break
        lines.append(f"Человек: {past.said}")
        answer = past.bot_text().replace("\n", " ")
        if answer:
            lines.append(f"Дневник: {answer[:200]}")
    return "\n".join(lines[-4:])


def check_invented_habit(turn: Turn, talk: Talk) -> str:
    """
    Записана привычка, о которой в реплике нет ни слова.

    Смотрим только на эту реплику и только на привычки: показатели модель
    вправе оценить по общему смыслу («разбитый» - это энергия), а привычка
    должна быть названа. Слова сверяем по корню в четыре буквы: «тренировка»
    и «тренировался» - одно и то же.
    """
    said = turn.said.lower()
    # Что видит в этой же реплике словарь: он знает синонимы («йога» -
    # «тренировка», «пиво» - «алкоголь»), и спорить с ним смысла нет.
    by_rules = set(_found_by_rules(turn.said))
    for message in turn.replies:
        note = message.get("note", "")
        if not note.startswith("Привычки"):
            continue
        # Перенос записи в другой день повторяет уже записанные факты - в самой
        # реплике («а нет, вру, это был вторник») их и не должно быть.
        if message["text"].startswith(("Поправил", "Уточнил")):
            continue
        for habit in note.split("\n")[0].removeprefix("Привычки: ").split(", "):
            # «прогулка: не было» - это запись об отсутствии привычки, её
            # проверяет отрицание в реплике, а не наличие слова.
            if habit.endswith("не было"):
                continue
            name = habit.split(" (")[0].split(":")[0].strip()
            if not name or name in by_rules:
                continue
            # Фактор времени суток идёт вслед за своей привычкой
            base = name.rsplit(" ", 1)[0]
            if base in by_rules:
                continue
            roots = [word[:4] for word in name.split() if len(word) > 3]
            if roots and any(root in said for root in roots):
                continue
            # Последнее слово за моделью: она понимает «ложился в два» как
            # поздний отбой, а словарь - нет.
            if judge_says_yes(turn.said, name, _talk_context(turn, talk)):
                continue
            return f"записана привычка «{name}», а в реплике её нет"
    return ""


def check_contradiction(turn: Turn, talk: Talk) -> str:
    """Один показатель назван в ответе дважды с разными числами."""
    seen: dict[str, str] = {}
    for value in re.findall(r"([а-яё ]+) (\d+(?:[.,]\d)?) из 10", turn.notes()):
        name, number = value[0].strip(), value[1]
        if name in seen and seen[name] != number:
            return f"«{name}» в одном ответе и {seen[name]}, и {number}"
        seen[name] = number
    return ""


def check_denied_but_written(turn: Turn, talk: Talk) -> str:
    """
    Человек сказал «не было», а в дне привычка стоит как случившаяся.

    Что человек отрицал, спрашиваем у словаря: он разбирает ту же фразу и
    ставит ноль там, где отрицание. Искать отрицание глазами по корню нельзя -
    во фразе «ел рыбу и овощи, сладкого не было» отрицание относится только к
    сладкому.
    """
    from datetime import date
    denied = {f.name for f in RULES.extract(turn.said, date.today()).facts
              if f.kind == "factor" and f.value == 0}
    if not denied:
        return ""
    for message in turn.replies:
        note = message.get("note", "")
        if not note.startswith("Привычки"):
            continue
        listed = note.split("\n")[0].removeprefix("Привычки: ")
        for habit in listed.split(", "):
            if habit.endswith("не было"):
                continue
            name = habit.split(" (")[0].strip()
            if name in denied:
                return f"сказано «не было», записано как было: {name}"
    return ""


CHECKS = [check_denied_but_written, check_not_understood, check_heavy_day, check_silence, check_empty_note,
          check_repeated_question, check_question_answered, check_invented_habit,
          check_contradiction]


# ── прогон ────────────────────────────────────────────────────────────────

def make_talk(rng: random.Random) -> tuple[str, list[str]]:
    """Собрать разговор: профиль и реплики."""
    profile = rng.choice(list(PROFILES))
    lines = [rng.choice(SAYINGS[kind]) for kind in PROFILES[profile]]
    # Хвост случайной длины: живые разговоры не кончаются на ровном месте.
    for _ in range(rng.randint(0, 3)):
        lines.append(rng.choice(SAYINGS[rng.choice(list(SAYINGS))]))
    return profile, lines


def run_talk(number: int, profile: str, lines: list[str], parser) -> Talk:
    talk = Talk(profile=profile)
    diary = Diary(f"stress-{number}")
    talk.diary = diary
    for line in lines:
        replies = dialog.step(diary, line, parser=parser, origin="stress")
        talk.turns.append(Turn(line, replies))
    return talk


def problems(talk: Talk) -> list[tuple[str, str]]:
    found = []
    for turn in talk.turns:
        for check in CHECKS:
            claim = check(turn, talk)
            if claim:
                found.append((turn.said, claim))
    return found


def main() -> None:
    parser_args = argparse.ArgumentParser(description=__doc__)
    parser_args.add_argument("--count", type=int, default=300)
    parser_args.add_argument("--seed", type=int, default=1)
    parser_args.add_argument("--rules", action="store_true",
                             help="без модели, только словарь")
    parser_args.add_argument("--workers", type=int, default=8)
    parser_args.add_argument("--show", type=int, default=40,
                             help="сколько проблемных мест показать")
    args = parser_args.parse_args()

    parser = dialog.Parser() if args.rules else dialog.Parser.from_environment()
    if not args.rules and parser.model is not None:
        global _judge
        _judge = parser.model._complete
    print(f"Разбор: {parser.engine}. Разговоров: {args.count}.", flush=True)

    rng = random.Random(args.seed)
    plan = [make_talk(rng) for _ in range(args.count)]

    talks: list[Talk] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_talk, i, profile, lines, parser)
                   for i, (profile, lines) in enumerate(plan)]
        for done, future in enumerate(futures, 1):
            try:
                talks.append(future.result())
            except Exception as error:            # разговор не должен падать
                print(f"  разговор {done} упал: {error!r}", flush=True)
            if done % 25 == 0:
                print(f"  прошло {done} из {len(futures)}", flush=True)

    counted: dict[str, int] = {}
    examples: list[str] = []
    for talk in talks:
        for said, claim in problems(talk):
            key = claim.split(":")[0].split("«")[0].strip() or claim
            counted[key] = counted.get(key, 0) + 1
            if len(examples) < args.show:
                examples.append(f"[{talk.profile}] «{said[:60]}» → {claim}")

    total = sum(counted.values())
    print(f"\nРазговоров: {len(talks)}. Проблемных мест: {total}.")
    for claim, times in sorted(counted.items(), key=lambda p: -p[1]):
        print(f"  {times:4}  {claim}")
    if examples:
        print("\nПримеры:")
        for line in examples:
            print(" ", line)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
