"""
Локальная страница, чтобы прототип можно было потрогать руками.

Запуск:  python3 -m web.server
Откроется на http://127.0.0.1:8765 — ни ключей, ни интернета не нужно.

Наружу ничего не отдаём и никуда не ходим: сервер слушает только 127.0.0.1.
"""
from __future__ import annotations

import json
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

from demo.generate import build
from wam.experiments import Experiment, evaluate
from wam.derive import derive_factors
from wam.extract import RuleExtractor
from wam.insights import find_links
from wam.schema import Timeline
from wam.wearables import SberRingSource, merge_into
from wam.derive import DEVICE_SOURCES

HOST, PORT = "127.0.0.1", 8765

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Что на меня влияет — прототип</title>
<style>
 :root{--ink:#1A1A1A;--text:#4B4F52;--soft:#8A8F93;--line:#E6E8E9;--green:#1F8A5B;--bg:#fff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
 .wrap{max-width:820px;margin:0 auto;padding:48px 24px 80px}
 .eyebrow{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:700;letter-spacing:.13em;
  text-transform:uppercase;color:var(--green);margin:0 0 18px}
 .eyebrow i{width:7px;height:7px;border-radius:50%;background:var(--green)}
 h1{font-size:38px;line-height:1.1;letter-spacing:-.02em;font-weight:800;margin:0 0 14px}
 h2{font-size:19px;margin:38px 0 10px}
 p{color:var(--text);margin:0 0 16px}
 textarea{width:100%;min-height:96px;padding:14px;font:inherit;border:1px solid var(--line);border-radius:8px;resize:vertical}
 button{font:inherit;font-weight:600;background:var(--green);color:#fff;border:0;border-radius:8px;padding:11px 22px;cursor:pointer}
 button.ghost{background:#fff;color:var(--green);border:1px solid var(--green)}
 .row{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
 .out{margin-top:22px;border-top:1px solid var(--line);padding-top:20px}
 .fact{display:flex;gap:12px;padding:9px 0;border-bottom:1px solid var(--line);font-size:15px}
 .fact b{min-width:150px}
 .fact span{color:var(--text)}
 .tag{font-size:12px;color:var(--soft);min-width:70px}
 .link{padding:14px 16px;border-left:3px solid var(--green);background:#F7F8F8;margin-bottom:10px;font-size:15px}
 .link em{font-style:normal;color:var(--soft);font-size:13px;display:block;margin-top:4px}
 .hint{font-size:14px;color:var(--soft)}
 .ex{border:1px solid var(--line);border-radius:8px;padding:16px 18px;margin-top:12px}
 .ex li{color:var(--text);font-size:15px;margin-bottom:6px}
 .verdict{font-weight:700;color:var(--green);margin-top:10px}
 .ring{margin-top:22px;padding:16px 18px;border:1px solid var(--line);border-radius:8px}
 .ring b{font-size:15px}
 .ringrow{display:flex;gap:22px;flex-wrap:wrap;margin-top:10px}
 .ringrow label{font-size:14px;color:var(--text);display:flex;align-items:center;gap:8px}
 .ringrow input{width:120px}
 .ringrow span{font-weight:700;color:var(--ink);min-width:42px}
 .src{font-size:12px;color:var(--soft);min-width:74px}
 .note{font-size:13px;color:var(--soft);display:block;margin-top:4px}
 .warn{color:#A5372F}
 .banner{background:#FBF6E7;border-bottom:1px solid #EFE3BE;color:#6B5A22;font-size:14px;
  padding:12px 24px;text-align:center;line-height:1.45}
 .known{margin-top:18px;padding:16px 18px;background:#F4F9F6;border-left:3px solid var(--green)}
 .known b{font-size:15px;display:block;margin-bottom:8px}
 .known div{font-size:14.5px;color:var(--text);margin-bottom:6px}
 .known .none{color:var(--soft)}
 .sources{display:flex;flex-direction:column;gap:8px;margin-top:12px}
 .sources label{display:flex;align-items:baseline;gap:10px;font-size:14.5px}
 .sources label.off{color:var(--soft)}
 .sources em{font-style:normal;font-size:13px;color:var(--soft)}
 .sources label.on span{font-weight:600;color:var(--ink)}
</style></head><body>
<div class="banner">Это прототип для заявки. Дневник за 120 дней придуман для показа,
  связи в нём заложены заранее — так видно, что программа находит настоящее и отсеивает случайное.</div>
<div class="wrap">
  <p class="eyebrow"><i></i>Прототип</p>
  <h1>Что на меня влияет</h1>
  <p>Напишите, как прошёл день — обычными словами, как в переписке. Программа разберёт
     фразу на привычки и самочувствие. Всё считается тут же на вашем компьютере.</p>

  <textarea id="text">Опять пил кофе часов в пять вечера, потом до ночи листал ленту. Спал часов пять, с утра тревога какая-то.</textarea>
  <div class="row">
    <button onclick="parse()">Разобрать запись</button>
    <button class="ghost" id="mic" onclick="listen()">🎤 Наговорить</button>
    <button class="ghost" onclick="demo()">Показать выводы за 4 месяца</button>
  </div>
  <p class="hint" id="micnote" style="margin-top:10px;min-height:20px"></p>
  <p class="hint">В продукте это голосовое в мессенджере. Здесь речь распознаёт сам браузер,
     нужен Chrome и интернет. Попробуйте: «сходил в зал, вечером бодрый»,
     «перелёт, спал четыре часа», «не пил кофе, выспался отлично».</p>

  <div class="ring">
    <b>Источники данных</b>
    <div class="sources">
      <label class="on"><input type="checkbox" id="src-ring" checked onchange="parse()">
        <span>Умное кольцо Sber</span><em>сон, стресс, шаги</em></label>
      <label class="off"><input type="checkbox" disabled>
        <span>Apple Health</span><em>разбор написан, подключение в программе</em></label>
      <label class="off"><input type="checkbox" disabled>
        <span>Health Connect</span><em>для Android, тот же формат</em></label>
      <label class="off"><input type="checkbox" disabled>
        <span>Календарь</span><em>встречи и перелёты как факторы</em></label>
      <label class="off"><input type="checkbox" disabled>
        <span>Погода и город</span><em>давление, смена часового пояса</em></label>
    </div>

    <b style="display:block;margin-top:18px">Что кольцо намеряло за этот день</b>
    <div class="ringrow">
      <label>Сон <input type="range" id="sleep" min="0" max="100" value="41" oninput="ringLabel()"><span id="sleepv">41</span></label>
      <label>Стресс <input type="range" id="stress" min="0" max="100" value="72" oninput="ringLabel()"><span id="stressv">72</span></label>
      <label>Шаги <input type="range" id="steps" min="0" max="20000" step="500" value="3000" oninput="ringLabel()"><span id="stepsv">3000</span></label>
    </div>
    <p class="hint" style="margin:10px 0 0">Эти цифры человек не вводит, они приходят с прибора.
       Снимите галочку с кольца и разберите запись заново — станет видно, сколько фактов
       пропадает без него.</p>
  </div>

  <div class="out" id="out"><p class="hint">Здесь появится результат.</p></div>
</div>
<script>
function ringLabel(){
  for(const id of ['sleep','stress','steps']) document.getElementById(id+'v').textContent = document.getElementById(id).value;
}
let rec = null, listening = false;
function micStatus(text){ document.getElementById('micnote').textContent = text || ''; }

function listen(){
  const mic = document.getElementById('mic');
  if(listening){ listening = false; rec && rec.stop(); return; }

  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if(!Rec){ micStatus('Этот браузер не умеет распознавать речь — откройте страницу в Chrome.'); return; }

  rec = new Rec();
  rec.lang = 'ru-RU';
  rec.interimResults = true;
  rec.continuous = true;            // иначе останавливается после первой же паузы

  let heard = '';
  rec.onstart = () => { listening = true; mic.textContent = '⏹ Стоп'; micStatus('Слушаю. Говорите, потом нажмите «Стоп».'); };
  rec.onresult = e => {
    let finalText = '', interim = '';
    for(let i = 0; i < e.results.length; i++){
      (e.results[i].isFinal ? finalText += e.results[i][0].transcript + ' ' : interim = e.results[i][0].transcript);
    }
    heard = (finalText + interim).trim();
    document.getElementById('text').value = heard;
  };
  rec.onerror = e => {
    listening = false;
    mic.textContent = '🎤 Наговорить';
    const reasons = {
      'not-allowed': 'Браузер не дал доступ к микрофону. Разрешите его в настройках сайта.',
      'service-not-allowed': 'Браузер не дал доступ к микрофону.',
      'no-speech': 'Не услышал речь. Нажмите ещё раз и говорите ближе к микрофону.',
      'audio-capture': 'Микрофон не найден.',
      'network': 'Распознаванию нужен интернет — оно идёт через сервис браузера.'
    };
    micStatus(reasons[e.error] || ('Не получилось: ' + e.error));
  };
  rec.onend = () => {
    if(listening){ rec.start(); return; }   // пауза в речи — продолжаем слушать
    mic.textContent = '🎤 Наговорить';
    if(heard){ micStatus('Записал: «' + heard + '»'); parse(); }
    else micStatus('');
  };
  rec.start();
}
async function parse(){
  const text = document.getElementById('text').value;
  const ring = document.getElementById('src-ring').checked ? {
    sleep_score: +document.getElementById('sleep').value,
    stress_level: +document.getElementById('stress').value,
    steps: +document.getElementById('steps').value
  } : null;
  const r = await fetch('/parse', {method:'POST', body: JSON.stringify({text, ring})});
  const d = await r.json();
  const out = document.getElementById('out');
  if(!d.facts.length){ out.innerHTML = '<p class=hint>Ничего не распознал. Попробуйте назвать привычку и самочувствие: «пил кофе, спал пять часов».</p>'; return; }
  const known = d.known.length
    ? d.known.map(k => `<div>${k.text}<span class="note ${k.warn ? 'warn' : ''}">${k.cause}</span></div>`).join('')
    : '<div class="none">Про эти привычки в дневнике пока нечего сказать: нужно минимум семь дней с ними и семь без.</div>';

  out.innerHTML = '<h2 style="margin-top:0">Что получилось за день</h2>' + d.facts.map(f =>
    `<div class="fact"><span class="src">${f.source}</span><span class="tag">${f.kind === 'factor' ? 'привычка' : 'состояние'}</span>
     <b>${f.name}</b><span>${f.kind === 'factor' ? (f.value ? 'было' : 'не было') : f.value + ' из 10'}</span></div>`).join('') +
    `<div class="known"><b>Что об этом уже известно из вашего дневника</b>${known}</div>` +
    '<p class="hint" style="margin-top:14px">Одна запись ничего не доказывает. Выводы появляются, когда таких дней набирается много — нажмите «Показать выводы за 120 дней».</p>';
}
async function demo(){
  const out = document.getElementById('out');
  out.innerHTML = '<p class=hint>Считаю…</p>';
  const r = await fetch('/demo');
  const d = await r.json();
  out.innerHTML = '<h2 style="margin-top:0">Что нашлось за 4 месяца дневника</h2>' +
    d.links.map(l => `<div class="link">${l.text}<em>${l.detail}</em><span class="note ${l.warn ? 'warn' : ''}">${l.cause}</span></div>`).join('') +
    `<h2>Проверка самой сильной связи</h2><div class="ex"><b>${d.experiment.hypothesis}</b><ul>` +
    d.experiment.plan.map(s => `<li>${s}</li>`).join('') +
    `</ul><div class="verdict">${d.verdict.status}</div><p style="margin:6px 0 0">${d.verdict.text}</p></div>` +
    '<p class="hint" style="margin-top:16px">Дневник придуман для показа: 120 дней, то есть около четырёх месяцев. Столько нужно, чтобы проверить третий фактор — дни делятся на части, и в каждой должно остаться достаточно наблюдений. Первые выводы у живого человека появляются раньше, недели через три: для простой связи хватает семи дней с привычкой и семи без.</p>';
}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/demo":
            self._send(json.dumps(_demo(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/parse":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        today = date.today()

        timeline = Timeline()
        timeline.add(RuleExtractor().extract(payload.get("text", ""), today))

        ring = payload.get("ring") or {}
        if ring:
            ring["date"] = today.isoformat()
            merge_into(timeline, SberRingSource().read([ring]))
            derive_factors(timeline)

        facts = [{"kind": f.kind, "name": f.name, "value": f.value,
                  "source": "с кольца" if f.source in DEVICE_SOURCES else "из рассказа"}
                 for f in timeline.days[0].facts]

        mentioned = {f.name for f in timeline.days[0].facts
                     if f.kind == "factor" and f.value > 0}
        known = [{
            "text": f"«{l.factor}» → «{l.metric}»: {l.direction} на {abs(l.effect):.1f} балла "
                    f"({'на следующий день' if l.lag_days else 'в тот же день'}, {l.strength})",
            "cause": l.causal_note,
            "warn": bool(l.confounder),
        } for l in _known_links() if l.factor in mentioned and l.strength != "наблюдение"]
        self._send(json.dumps({"facts": facts, "known": known}, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *args):
        pass  # не засоряем терминал

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_CACHE: dict[str, object] = {}


def _known_timeline():
    """Дневник за 120 дней считаем один раз: перестановочный тест не быстрый."""
    if "timeline" not in _CACHE:
        _CACHE["timeline"] = derive_factors(build(days=120))
    return _CACHE["timeline"]


def _known_links():
    if "links" not in _CACHE:
        _CACHE["links"] = find_links(_known_timeline())
    return _CACHE["links"]


def _demo() -> dict:
    timeline = _known_timeline()
    links = [l for l in _known_links() if l.strength != "наблюдение"]
    experiment = Experiment.from_link(links[0], start=timeline.days[-40].day, days=40)
    verdict = evaluate(experiment, timeline)
    return {
        "links": [{
            "text": f"«{l.factor}» → «{l.metric}»: {l.direction} на {abs(l.effect):.1f} балла",
            "detail": f"{'в тот же день' if l.lag_days == 0 else 'на следующий день'} · "
                      f"{l.days_with} дней с фактором против {l.days_without} без · "
                      f"{l.strength} · источник: {l.source}",
            "cause": l.causal_note,
            "warn": bool(l.confounder),
        } for l in links],
        "experiment": {"hypothesis": experiment.hypothesis, "plan": experiment.plan},
        "verdict": {"status": verdict.status.capitalize(), "text": verdict.text},
    }


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Откройте http://{HOST}:{PORT} — остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    main()
