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
from wam.extract import RuleExtractor
from wam.insights import find_links

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
</style></head><body>
<div class="wrap">
  <p class="eyebrow"><i></i>Прототип</p>
  <h1>Что на меня влияет</h1>
  <p>Напишите, как прошёл день — обычными словами, как в переписке. Программа разберёт
     фразу на привычки и самочувствие. Всё считается тут же на вашем компьютере.</p>

  <textarea id="text">Опять пил кофе часов в пять вечера, потом до ночи листал ленту. Спал часов пять, с утра тревога какая-то.</textarea>
  <div class="row">
    <button onclick="parse()">Разобрать запись</button>
    <button class="ghost" onclick="demo()">Показать выводы за 90 дней</button>
  </div>
  <p class="hint" style="margin-top:10px">Попробуйте свои фразы: «сходил в зал, вечером бодрый»,
     «перелёт, спал четыре часа», «не пил кофе, выспался отлично».</p>

  <div class="out" id="out"><p class="hint">Здесь появится результат.</p></div>
</div>
<script>
async function parse(){
  const text = document.getElementById('text').value;
  const r = await fetch('/parse', {method:'POST', body: JSON.stringify({text})});
  const d = await r.json();
  const out = document.getElementById('out');
  if(!d.facts.length){ out.innerHTML = '<p class=hint>Ничего не распознал. Попробуйте назвать привычку и самочувствие: «пил кофе, спал пять часов».</p>'; return; }
  out.innerHTML = '<h2 style="margin-top:0">Что понял из фразы</h2>' + d.facts.map(f =>
    `<div class="fact"><span class="tag">${f.kind === 'factor' ? 'привычка' : 'состояние'}</span>
     <b>${f.name}</b><span>${f.kind === 'factor' ? (f.value ? 'было' : 'не было') : f.value + ' из 10'}</span></div>`).join('');
}
async function demo(){
  const out = document.getElementById('out');
  out.innerHTML = '<p class=hint>Считаю…</p>';
  const r = await fetch('/demo');
  const d = await r.json();
  out.innerHTML = '<h2 style="margin-top:0">Что нашлось за 90 дней</h2>' +
    d.links.map(l => `<div class="link">${l.text}<em>${l.detail}</em></div>`).join('') +
    `<h2>Проверка самой сильной связи</h2><div class="ex"><b>${d.experiment.hypothesis}</b><ul>` +
    d.experiment.plan.map(s => `<li>${s}</li>`).join('') +
    `</ul><div class="verdict">${d.verdict.status}</div><p style="margin:6px 0 0">${d.verdict.text}</p></div>` +
    '<p class="hint" style="margin-top:16px">Дневник для примера сгенерирован. В него заранее заложены две настоящие связи и один фактор-пустышка — «сладкое». Программа должна найти первые две и промолчать про третий.</p>';
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
        record = RuleExtractor().extract(payload.get("text", ""), date.today())
        facts = [{"kind": f.kind, "name": f.name, "value": f.value} for f in record.facts]
        self._send(json.dumps({"facts": facts}, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *args):
        pass  # не засоряем терминал

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _demo() -> dict:
    timeline = build(days=90)
    links = find_links(timeline)
    experiment = Experiment.from_link(links[0], start=timeline.days[-40].day, days=40)
    verdict = evaluate(experiment, timeline)
    return {
        "links": [{
            "text": f"«{l.factor}» → «{l.metric}»: {l.direction} на {abs(l.effect):.1f} балла",
            "detail": f"{'в тот же день' if l.lag_days == 0 else 'на следующий день'} · "
                      f"{l.days_with} дней с привычкой против {l.days_without} без · {l.strength}",
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
