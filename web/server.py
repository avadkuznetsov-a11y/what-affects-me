"""
Локальная страница-диалог: так же, как продукт будет работать в мессенджере.

Запуск:  python3 -m web.server
Откроется на http://127.0.0.1:8765 - ни ключей, ни интернета не нужно.

Человек пишет про свой день, программа отвечает: что записала, чего не хватает
и что уже известно. Когда данных достаточно, показывает выводы. Сервер слушает
только 127.0.0.1 и наружу ничего не отдаёт.
"""
from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from demo.generate import build
from wam.derive import DEVICE_SOURCES, derive_factors
from wam.experiments import Experiment, evaluate
from wam.extract import LLMExtractor, RuleExtractor
from wam.llm import available_engine
from wam.insights import find_links
from wam.phrases import basis, next_step, say
from wam.questions import apply_answer, next_question
from wam.schema import DayRecord, Timeline
from wam.wearables import SberRingSource, merge_into

HOST, PORT = "127.0.0.1", 8765
DEFAULT_DAYS = 120

# Речь разбирает модель, если для неё есть ключ в окружении. Ключей в
# репозитории нет: у того, кто скачает код, будет работать разбор по правилам,
# пока он не подставит свой ключ.
ENGINE_NAME, _COMPLETE = available_engine()
_RULES = RuleExtractor()
_MODEL = LLMExtractor(_COMPLETE) if ENGINE_NAME != "правила" else None


def parse_day(text: str, day):
    """Разбор фразы: сначала модель, при сбое или пустом ответе - правила."""
    if _MODEL is not None:
        try:
            record = _MODEL.extract(text, day)
            if record.facts:
                return record, ENGINE_NAME
        except Exception:
            pass
    return _RULES.extract(text, day), "правила"

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Что на меня влияет - прототип</title>
<style>
 :root{--ink:#1A1A1A;--text:#4B4F52;--soft:#8A8F93;--line:#E6E8E9;--green:#1F8A5B;--bg:#fff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
 .banner{background:#FBF6E7;border-bottom:1px solid #EFE3BE;color:#6B5A22;font-size:14px;padding:11px 24px;text-align:center}
 .wrap{max-width:760px;margin:0 auto;padding:40px 24px 60px}
 .eyebrow{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:700;letter-spacing:.13em;
  text-transform:uppercase;color:var(--green);margin:0 0 16px}
 .eyebrow i{width:7px;height:7px;border-radius:50%;background:var(--green)}
 h1{font-size:34px;line-height:1.1;letter-spacing:-.02em;font-weight:800;margin:0 0 12px}
 p{color:var(--text);margin:0 0 14px}
 .hint{font-size:14px;color:var(--soft)}

 .chat{border:1px solid var(--line);border-radius:12px;margin-top:22px;overflow:hidden}
 .feed{padding:18px;display:flex;flex-direction:column;gap:12px;max-height:58vh;overflow-y:auto}
 .msg{max-width:88%;padding:11px 14px;border-radius:14px;font-size:15px;line-height:1.45;white-space:pre-line}
 .msg.me{align-self:flex-end;background:var(--green);color:#fff}
 .msg.bot{background:#F4F6F7}
 .msg.ask{background:#FBF6E7;border-left:3px solid #D9B84C}
 .msg.result{background:#F1F8F4;border-left:3px solid var(--green)}
 .msg small{display:block;margin-top:6px;color:var(--soft);font-size:13px}
 .msg.me small{color:rgba(255,255,255,.75)}
 .bar{display:flex;gap:10px;padding:14px 18px;border-top:1px solid var(--line);background:#FAFBFB}
 .bar input{flex:1;padding:12px 14px;font:inherit;border:1px solid var(--line);border-radius:10px}
 button{font:inherit;font-weight:600;background:var(--green);color:#fff;border:0;border-radius:10px;padding:12px 20px;cursor:pointer}
 button.ghost{background:#fff;color:var(--green);border:1px solid var(--green)}
 button:disabled{opacity:.5;cursor:default}

 .side{margin-top:26px;padding:18px;border:1px solid var(--line);border-radius:12px}
 .side b{font-size:15px}
 .sources{display:flex;flex-direction:column;gap:7px;margin:12px 0 18px}
 .sources label{display:flex;align-items:baseline;gap:9px;font-size:14px;color:var(--soft)}
 .sources label.on{color:var(--ink)}
 .sources em{font-style:normal;font-size:13px;color:var(--soft)}
 .ringrow{display:flex;gap:20px;flex-wrap:wrap}
 .ringrow label{font-size:14px;color:var(--text);display:flex;align-items:center;gap:8px}
 .ringrow input{width:110px}
 .ringrow span{font-weight:700;min-width:44px}
 .tools{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px;align-items:center}
 select{font:inherit;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:#fff}
 pre.keys{margin:10px 0 0;padding:12px 14px;background:#F4F6F7;border-radius:8px;overflow-x:auto;
  font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12.5px;line-height:1.7;color:#3A3F42}
</style></head><body>
<div class="banner">Это прототип для заявки. Дневник за выбранный срок придуман для показа:
 связи в нём заложены заранее, чтобы было видно, что программа находит настоящее и отсеивает случайное.</div>

<div class="wrap">
  <p class="eyebrow"><i></i>Прототип · разбор речи: __ENGINE__</p>
  <h1>Что на меня влияет</h1>
  <p>Расскажите про свой день обычными словами - так же, как написали бы в мессенджере.
     Программа спросит, если чего-то не хватает, и скажет, что уже знает про ваши привычки.</p>

  <div class="chat">
    <div class="feed" id="feed"></div>
    <div class="bar">
      <input id="text" placeholder="Например: пил кофе часов в пять, спал часов пять" onkeydown="if(event.key==='Enter')send()">
      <button class="ghost" id="mic" onclick="listen()" title="Наговорить">🎤</button>
      <button id="send" onclick="send()">Отправить</button>
    </div>
  </div>
  <p class="hint" id="micnote" style="min-height:18px;margin-top:8px"></p>

  <div class="side">
    <b>Источники данных</b>
    <div class="sources">
      <label class="on"><input type="checkbox" id="src-ring" checked><span>Умное кольцо Sber</span><em>сон, стресс, шаги</em></label>
      <label><input type="checkbox" disabled><span>Apple Health</span><em>разбор написан, подключение в программе</em></label>
      <label><input type="checkbox" disabled><span>Health Connect</span><em>для Android, тот же формат</em></label>
      <label><input type="checkbox" disabled><span>Календарь</span><em>встречи и перелёты как факторы</em></label>
      <label><input type="checkbox" disabled><span>Погода и город</span><em>давление, смена часового пояса</em></label>
    </div>
    <b>Что кольцо намеряло сегодня</b>
    <div class="ringrow" style="margin-top:10px">
      <label>Сон <input type="range" id="sleep" min="0" max="100" value="41" oninput="lbl()"><span id="sleepv">41</span></label>
      <label>Стресс <input type="range" id="stress" min="0" max="100" value="72" oninput="lbl()"><span id="stressv">72</span></label>
      <label>Шаги <input type="range" id="steps" min="0" max="20000" step="500" value="3000" oninput="lbl()"><span id="stepsv">3000</span></label>
    </div>
    <b style="display:block;margin-top:20px">Чем разбирается речь</b>
    <p class="hint" style="margin-top:8px">Сейчас: <b id="engine">__ENGINE__</b>.
      Разбор по правилам работает всегда и без интернета, но понимает только знакомые слова.
      Чтобы разбирала модель, запустите с собственным ключом - он читается из окружения
      и никуда не сохраняется:</p>
    <pre class="keys">GIGACHAT_TOKEN=... python3 -m web.server
YANDEX_API_KEY=... YANDEX_FOLDER_ID=... python3 -m web.server
ANTHROPIC_API_KEY=... python3 -m web.server</pre>

    <div class="tools">
      <select id="days">
        <option value="21">дневник за 3 недели</option>
        <option value="45">за 1,5 месяца</option>
        <option value="90">за 3 месяца</option>
        <option value="120" selected>за 4 месяца</option>
        <option value="180">за полгода</option>
      </select>
      <button class="ghost" onclick="summary()">Показать все выводы</button>
      <button class="ghost" onclick="reset()">Начать заново</button>
    </div>
  </div>
</div>

<script>
function lbl(){ for(const id of ['sleep','stress','steps']) document.getElementById(id+'v').textContent = document.getElementById(id).value; }

function add(kind, text, note){
  const feed = document.getElementById('feed');
  const div = document.createElement('div');
  div.className = 'msg ' + kind;
  div.textContent = text;
  if(note){ const s = document.createElement('small'); s.textContent = note; div.appendChild(s); }
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

async function send(){
  const field = document.getElementById('text');
  const text = field.value.trim();
  if(!text) return;
  add('me', text);
  field.value = '';
  document.getElementById('send').disabled = true;

  const ring = document.getElementById('src-ring').checked ? {
    sleep_score: +document.getElementById('sleep').value,
    stress_level: +document.getElementById('stress').value,
    steps: +document.getElementById('steps').value
  } : null;

  try {
    const r = await fetch('/say', {method:'POST', body: JSON.stringify({text, ring, days: +document.getElementById('days').value})});
    const d = await r.json();
    for(const m of d.messages) add(m.kind, m.text, m.note);
  } catch(e) {
    add('bot', 'Не получилось связаться с программой: ' + e.message);
  }
  document.getElementById('send').disabled = false;
  field.focus();
}

async function summary(){
  add('me', 'Что уже известно?');
  const r = await fetch('/summary?days=' + document.getElementById('days').value);
  const d = await r.json();
  for(const m of d.messages) add(m.kind, m.text, m.note);
}

async function reset(){
  await fetch('/reset');
  document.getElementById('feed').innerHTML = '';
  hello();
}

function hello(){
  add('bot', 'Расскажите, как прошёл день. Например: «пил кофе часов в пять, спал часов пять, с утра тревожно».');
}

let rec = null, listening = false;
function listen(){
  const mic = document.getElementById('mic');
  const note = document.getElementById('micnote');
  if(listening){ listening = false; rec && rec.stop(); return; }
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if(!Rec){ note.textContent = 'Этот браузер не умеет распознавать речь - откройте страницу в Chrome.'; return; }
  rec = new Rec(); rec.lang = 'ru-RU'; rec.interimResults = true; rec.continuous = true;
  let heard = '';
  rec.onstart = () => { listening = true; mic.textContent = '⏹'; note.textContent = 'Слушаю. Говорите, потом нажмите стоп.'; };
  rec.onresult = e => {
    let finalText = '', interim = '';
    for(let i = 0; i < e.results.length; i++){
      e.results[i].isFinal ? finalText += e.results[i][0].transcript + ' ' : interim = e.results[i][0].transcript;
    }
    heard = (finalText + interim).trim();
    document.getElementById('text').value = heard;
  };
  rec.onerror = e => {
    listening = false; mic.textContent = '🎤';
    const reasons = {'not-allowed':'Браузер не дал доступ к микрофону.','no-speech':'Не услышал речь, попробуйте ещё раз.',
      'audio-capture':'Микрофон не найден.','network':'Распознаванию нужен интернет.'};
    note.textContent = reasons[e.error] || ('Не получилось: ' + e.error);
  };
  rec.onend = () => {
    if(listening){ rec.start(); return; }
    mic.textContent = '🎤'; note.textContent = '';
    if(heard) send();
  };
  rec.start();
}

hello();
</script></body></html>"""


# ── состояние разговора ───────────────────────────────────────────────────

class Session:
    """Один разговор: сегодняшняя запись и вопрос, на который ждём ответ."""

    def __init__(self) -> None:
        self.record = DayRecord(day=date.today())
        self.pending: str | None = None

    def reset(self) -> None:
        self.record = DayRecord(day=date.today())
        self.pending = None


SESSION = Session()
_CACHE: dict[int, tuple] = {}


def _diary(days: int = DEFAULT_DAYS):
    """Придуманный дневник и найденные в нём связи. Считаем один раз на срок."""
    if days not in _CACHE:
        timeline = derive_factors(build(days=days))
        _CACHE[days] = (timeline, find_links(timeline))
    return _CACHE[days]


def _facts_note(record: DayRecord) -> str:
    """
    Показываем только то, что было, и сами показатели. Строки вида
    «много двигался: не было» человеку не нужны - это шум.
    """
    habits, metrics = [], []
    for fact in record.facts:
        mark = " (с кольца)" if fact.source in DEVICE_SOURCES else ""
        if fact.kind == "factor":
            if fact.value > 0:
                habits.append(f"{fact.name}{mark}")
            elif fact.source not in DEVICE_SOURCES:
                habits.append(f"{fact.name}: не было")
        else:
            metrics.append(f"{fact.name} {fact.value:g} из 10{mark}")

    lines = []
    if habits:
        lines.append("Привычки: " + ", ".join(dict.fromkeys(habits)))
    if metrics:
        lines.append("Самочувствие: " + ", ".join(dict.fromkeys(metrics)))
    return "\n".join(lines)


def _handle(text: str, ring: dict | None, days: int) -> list[dict]:
    """Один шаг разговора. Возвращает сообщения для ленты."""
    _, links = _diary(days)
    messages: list[dict] = []

    if SESSION.pending:
        # Это ответ на заданный вопрос: он дополняет сегодняшнюю запись
        before = _metrics_snapshot(SESSION.record)
        apply_answer(SESSION.record, SESSION.pending, text)
        if _metrics_snapshot(SESSION.record) == before:
            # ответ не понят - разбираем его как обычную фразу
            parsed, engine = parse_day(text, SESSION.record.day)
            for fact in parsed.facts:
                SESSION.record.add(fact)
        SESSION.pending = None
        engine = locals().get("engine", ENGINE_NAME)
    else:
        parsed, engine = parse_day(text, SESSION.record.day)
        for fact in parsed.facts:
            SESSION.record.add(fact)

    if ring:
        day_timeline = Timeline()
        day_timeline.add(SESSION.record)
        merge_into(day_timeline, SberRingSource().read([{**ring, "date": SESSION.record.day.isoformat()}]))
        derive_factors(day_timeline)

    if not SESSION.record.facts:
        SESSION.pending = next_question(SESSION.record)
        return [{"kind": "ask", "text": SESSION.pending}]

    messages.append({"kind": "bot", "text": f"Записал, разобрал {engine}:",
                     "note": _facts_note(SESSION.record)})

    question = next_question(SESSION.record, links)
    if question:
        SESSION.pending = question
        messages.append({"kind": "ask", "text": question})
        return messages

    messages.append({"kind": "bot", "text": "Картина дня понятна. Смотрю, что об этом "
                                            "говорит ваш дневник."})
    messages.extend(_conclusions(SESSION.record, links))
    return messages


def _metrics_snapshot(record: DayRecord) -> dict:
    return {f.name: f.value for f in record.facts if f.kind == "metric"}


def _conclusions(record: DayRecord, links) -> list[dict]:
    mentioned = {f.name for f in record.facts if f.kind == "factor" and f.value > 0}
    found = [l for l in links if l.factor in mentioned and l.strength != "наблюдение"]
    if not found:
        return [{"kind": "bot", "text": "Про эти привычки выводов пока нет: нужно хотя бы семь "
                                        "дней с ними и семь без. Продолжайте записывать."}]
    return [{"kind": "result", "text": say(l), "note": f"{basis(l)} {next_step(l)}"} for l in found]


def _summary(days: int) -> list[dict]:
    timeline, links = _diary(days)
    strong = [l for l in links if l.strength != "наблюдение"]
    period = f"{days} дней" if days < 60 else f"{round(days / 30)} месяца"

    if not strong:
        return [{"kind": "bot", "text": f"За {period} ничего надёжного не набралось. "
                                        "Это тоже ответ: случайных совпадений не показываю."}]

    messages = [{
        "kind": "bot",
        "text": f"За {period} проверено {len(links)} пар «привычка - состояние». "
                f"Осталось {len(strong)}, из них {sum(1 for l in strong if l.confounder)} "
                f"объясняются другим фактором.",
    }]
    messages += [{"kind": "result", "text": say(l), "note": f"{basis(l)} {next_step(l)}"}
                 for l in strong]

    best = next((l for l in strong if not l.confounder), None)
    if best:
        window = min(40, max(14, days // 3))
        experiment = Experiment.from_link(best, start=timeline.days[-window].day, days=window)
        verdict = evaluate(experiment, timeline)
        messages.append({"kind": "bot",
                         "text": "Проверка сильнейшей связи:\n" + "\n".join(experiment.plan),
                         "note": verdict.text})
    return messages


# ── HTTP ──────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            SESSION.reset()      # открыли страницу - разговор начинается с чистого листа
            self._send(PAGE.replace("__ENGINE__", ENGINE_NAME).encode("utf-8"),
                       "text/html; charset=utf-8")
        elif self.path.startswith("/summary"):
            self._json({"messages": _summary(_days_param(self.path))})
        elif self.path == "/reset":
            SESSION.reset()
            self._json({"ok": True})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/say":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        messages = _handle(payload.get("text", ""), payload.get("ring"),
                           int(payload.get("days") or DEFAULT_DAYS))
        self._json({"messages": messages})

    def log_message(self, *args):
        pass

    def _json(self, data):
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _days_param(path: str, default: int = DEFAULT_DAYS) -> int:
    if "days=" not in path:
        return default
    try:
        return max(14, min(365, int(path.split("days=")[1].split("&")[0])))
    except ValueError:
        return default


def main() -> None:
    threading.Thread(target=_diary, daemon=True).start()   # прогрев, чтобы первый ответ не ждал
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Откройте http://{HOST}:{PORT} - остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
