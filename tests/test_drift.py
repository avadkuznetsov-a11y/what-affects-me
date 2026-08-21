"""
Длительные отклонения: показатель ниже своей нормы неделями подряд.

Дневники тут набиваются руками и без случайности: отклонение либо есть в
данных, либо его нет, и тест должен ломаться от изменения порога, а не от
удачного посева. Сеть и разбор речи сюда не заходят вовсе.
"""
from datetime import date, timedelta

from wam.diary import Diary
from wam.drift import (DOCTOR_WEEKS, DRIFT_GAP, MIN_WEEKS, QUIET_DAYS, find,
                       notice, phrase)
from wam.schema import DayRecord, Fact, Timeline

TODAY = date(2026, 8, 20)

# Норму программа считает по прошлым дням человека, поэтому до провала должен
# быть кусок обычной жизни: без него отсчитывать не от чего.
NORMAL_DAYS = 30


def _line(values: dict[str, dict[int, float]]) -> Timeline:
    """
    Дневник из рядов «показатель -> {сколько дней назад: значение}».

    Так дни в тесте видно целиком, а не через генератор с арифметикой в индексе.
    """
    timeline = Timeline()
    days: dict[int, DayRecord] = {}
    for name, series in values.items():
        for ago, value in series.items():
            record = days.get(ago)
            if record is None:
                record = DayRecord(day=TODAY - timedelta(days=ago))
                days[ago] = record
                timeline.add(record)
            record.add(Fact("metric", name, float(value), "diary"))
    return timeline


def _steady(level: float, days: int = NORMAL_DAYS, since: int = 0) -> dict[int, float]:
    """Ровный ряд: одно и то же значение столько-то дней подряд."""
    return {ago: level for ago in range(since, since + days)}


def _dip(level: float, days: int, after: float = 7.0) -> dict[int, float]:
    """Провал последних дней на фоне обычной жизни до него."""
    return {**_steady(level, days), **_steady(after, NORMAL_DAYS, since=days)}


# ── когда отклонение есть ─────────────────────────────────────────────────

def test_month_below_own_norm_is_noticed():
    """Тот самый случай: месяц энергии ниже своей обычной."""
    found = find(_line({"энергия": _dip(4.0, days=28)}), TODAY)

    assert found is not None
    assert found.metric == "энергия" and found.worse
    assert found.weeks == 4
    assert found.norm - found.level >= DRIFT_GAP


def test_two_weeks_is_the_shortest_stretch_we_call_long():
    found = find(_line({"энергия": _dip(4.0, days=14)}), TODAY)
    assert found is not None and found.weeks == MIN_WEEKS


def test_one_bad_week_is_not_a_drift():
    """Неделя - это командировка или отчёт, а не отклонение."""
    assert find(_line({"энергия": _dip(4.0, days=7)}), TODAY) is None


def test_normal_diary_says_nothing():
    """Обычная жизнь с обычными колебаниями - молчим."""
    swings = {ago: 7.0 + (0.5 if ago % 2 else -0.5) for ago in range(60)}
    assert find(_line({"энергия": swings}), TODAY) is None


def test_a_dip_that_ended_is_not_a_drift():
    """Провал был, но кончился неделю назад - «держится» про него уже неправда."""
    series = {**_steady(7.0, 7), **_steady(4.0, 21, since=7), **_steady(7.0, 30, since=28)}
    assert find(_line({"энергия": series}), TODAY) is None


def test_no_norm_no_drift():
    """
    Дневник, в котором энергия низкая с самого начала: нормы человека мы не
    знаем, и назвать это «ниже вашей обычной» нельзя.
    """
    assert find(_line({"энергия": _steady(4.0, days=40)}), TODAY) is None


def test_a_broken_week_stops_the_count():
    """Одна неделя на своём уровне - и «подряд» кончилось на ней."""
    series = {**_steady(4.0, 14), **_steady(7.0, 7, since=14),
              **_steady(4.0, 7, since=21), **_steady(7.0, NORMAL_DAYS, since=28)}
    found = find(_line({"энергия": series}), TODAY)
    assert found is not None and found.weeks == 2


def test_a_week_almost_without_records_does_not_count():
    """По одной записи за неделю сказать, какой была неделя, нельзя."""
    series = {**_steady(4.0, 14), 20: 4.0, **_steady(7.0, NORMAL_DAYS, since=21)}
    found = find(_line({"энергия": series}), TODAY)
    assert found is not None and found.weeks == 2


def test_above_the_norm_is_noticed_too():
    found = find(_line({"энергия": _dip(9.0, days=21, after=5.0)}), TODAY)
    assert found is not None and not found.worse and found.weeks == 3


# ── что человек услышит ───────────────────────────────────────────────────

def test_sleep_in_norm_is_ruled_out():
    """Образец от заказчика: энергия просела, а сон при этом обычный."""
    line = _line({"энергия": _dip(4.0, days=28), "качество сна": _steady(7.0, 60)})
    said = phrase(find(line, TODAY))

    assert said.startswith("Энергия ниже вашей обычной четвёртую неделю подряд")
    assert "сон тут ни при чём - он в норме" in said
    assert "это не диагноз" in said.lower()
    assert "врачу" in said


def test_sleep_that_dropped_too_is_not_ruled_out():
    """Сон просел вместе с энергией - «он в норме» было бы враньём."""
    line = _line({"энергия": _dip(4.0, days=28), "качество сна": _dip(4.0, days=28)})
    assert "ни при чём" not in phrase(find(line, TODAY))


def test_a_short_drift_does_not_send_to_the_doctor():
    said = phrase(find(_line({"энергия": _dip(4.0, days=14)}), TODAY))
    assert "врачу" not in said
    assert "это не диагноз" in said.lower()


def test_no_medicine_in_the_words():
    """
    Граница, ради которой всё и написано: ни болезней, ни анализов, ни
    назначений. Отличить по дневнику анемию от щитовидки нельзя, и советовать
    «сдайте железо» мы не имеем права.
    """
    line = _line({"энергия": _dip(3.0, days=42), "качество сна": _steady(7.0, 80)})
    said = phrase(find(line, TODAY)).lower()

    for forbidden in ("железо", "витамин", "анализ", "щитовид", "анеми", "депресс",
                      "лекарств", "таблет", "препарат", "лечен", "обследов",
                      "вам следует", "вам нужно", "необходимо", "рекомендую"):
        assert forbidden not in said, forbidden


# ── как часто это говорится ───────────────────────────────────────────────

def _diary_with_drift() -> Diary:
    diary = Diary("test")
    diary.timeline = _line({"энергия": _dip(4.0, days=28),
                            "качество сна": _steady(7.0, 60)})
    return diary


def test_the_same_thing_is_not_said_twice():
    diary = _diary_with_drift()
    assert notice(diary, TODAY)
    assert notice(diary, TODAY) == ""


def test_quiet_for_a_few_days_after_saying_it():
    diary = _diary_with_drift()
    assert notice(diary, TODAY)
    for ahead in range(1, QUIET_DAYS):
        assert notice(diary, TODAY + timedelta(days=ahead)) == ""


def test_after_the_quiet_days_it_can_be_said_again():
    """
    Через несколько дней сказать можно - но только если фраза изменилась:
    слово в слово одно и то же держит `diary.observed`.
    """
    diary = _diary_with_drift()
    first = notice(diary, TODAY)
    later = TODAY + timedelta(days=QUIET_DAYS)
    diary.timeline.add(DayRecord(day=later, facts=[Fact("metric", "энергия", 4.0)]))

    said = notice(diary, later)
    assert said and said != first


def test_reset_forgets_that_it_was_said():
    diary = _diary_with_drift()
    notice(diary, TODAY)
    diary.reset()
    assert diary.drift_said is None


def test_doctor_is_offered_only_from_a_month():
    """Порог про врача - именно месяц, а не «сколько-нибудь недель»."""
    short = phrase(find(_line({"энергия": _dip(4.0, days=7 * (DOCTOR_WEEKS - 1))}), TODAY))
    long = phrase(find(_line({"энергия": _dip(4.0, days=7 * DOCTOR_WEEKS)}), TODAY))
    assert "врачу" not in short and "врачу" in long
