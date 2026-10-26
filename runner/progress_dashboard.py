"""Local read-only dashboard for the measured baseline pipeline progress.

Stdlib only. Polls the run directory and renders stage / usage / question
progress. Start:  uv run python runner/progress_dashboard.py --port 8765
"""
import argparse
import json
from pathlib import Path
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
STAGES = [
    "value_index", "few_shot_index", "value_retrieval", "few_shot_preparation",
    "schema_linking", "sql_generation", "sql_revision", "sql_selection",
]
PROGRESS_RE = re.compile(r"(\d+)/(\d+)\s*\(([\d.]+)%\)")
TQDM_RE = re.compile(r"\|\s*(\d+)/(\d+)\s*\[")
ITEM_DONE_RES = [
    re.compile(r"\[few_shot_preparation\]\[item (\w+)\] prepared"),
]


def read_jsonl(path):
    if not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def tail_text(path, n=4000):
    p = Path(path)
    if not p.exists():
        return ""
    with p.open("rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - n))
        return fh.read().decode("utf-8", errors="replace")


def scan_run(run):
    run = Path(run)
    stage_runs = {r["stage"]: r for r in read_jsonl(run / "measurement/stage_runs.jsonl")}
    usage = read_jsonl(run / "measurement/llm_usage.jsonl")
    chat = [r for r in usage if r.get("kind", "chat") == "chat"]
    emb = [r for r in usage if r.get("kind") == "embedding"]
    stages = []
    for name in STAGES:
        sr = stage_runs.get(name)
        prog = None
        text = tail_text(run / f"{name}.log", 6000)
        cands = []
        for m in PROGRESS_RE.finditer(text):
            a, b, pc = m.groups()
            cands.append((m.start(), int(a), int(b), float(pc)))
        for m in TQDM_RE.finditer(text):
            a, b = (int(x) for x in m.groups())
            cands.append((m.start(), a, b, 100.0 * a / b if b else 0))
        if cands:
            _, a, b, pc = max(cands, key=lambda x: (x[0], x[1]))
            prog = {"done": a, "total": b, "pct": pc}
        state = "pending"
        if sr:
            state = "running" if sr.get("status") == "running" else ("ok" if sr.get("status") == "completed" else "failed")
        elif (run / f"{name}.log").exists():
            state = "running"
        u = [r for r in usage if r.get("stage") == name]
        stages.append({"name": name, "state": state, "prog": prog,
                       "wall": sr.get("wall_seconds") if sr else None,
                       "error": sr.get("error") if sr else None,
                       "calls": len(u),
                       "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in u),
                       "completion_tokens": sum(r.get("completion_tokens") or 0 for r in u),
                       "total_tokens": sum(r.get("total_tokens") or 0 for r in u),
                       "finished_at": sr.get("finished_at") if sr else None})
    caps = sum(1 for r in chat if (r.get("completion_tokens") or 0) >= 8000)
    q = []
    for f in ("few_shot_preparation", "schema_linking", "sql_generation", "sql_revision", "sql_selection"):
        fp = run / f"{f}.snapshot.data/items.jsonl"
        if fp.exists() and len(q) == 0:
            for rec in read_jsonl(fp):
                it = rec.get("item", rec)
                q.append({"id": it.get("question_id"), "stage": f})
    return {"stages": stages,
            "usage": {"chat_calls": len(chat), "chat_tokens": sum(r.get("total_tokens") or 0 for r in chat),
                      "chat_prompt": sum(r.get("prompt_tokens") or 0 for r in chat),
                      "chat_completion": sum(r.get("completion_tokens") or 0 for r in chat),
                      "at_cap": caps, "embed_calls": len(emb),
                      "embed_tokens": sum(r.get("total_tokens") or 0 for r in emb)},
            "done": all(sr and sr.get("status") == "completed" for sr in (stage_runs.get(s) for s in STAGES))}


PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>基线评测进度</title><style>
:root{color-scheme:light}
*{box-sizing:border-box;margin:0;font-family:system-ui,"Microsoft YaHei",sans-serif}
body{background:#f5f7fa;color:#2c3a4b;padding:22px;max-width:960px;margin:auto}
h1{font-size:1.25rem;font-weight:600;margin-bottom:2px}
.sub{color:#7b8a9a;font-size:.8rem;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.card{background:#fff;border:1px solid #dfe6ee;border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:1.25rem;color:#1f4e79}
.card span{font-size:.74rem;color:#7b8a9a}
.stage{background:#fff;border:1px solid #dfe6ee;border-radius:10px;padding:11px 14px;margin-bottom:8px;
display:grid;grid-template-columns:180px 1fr 210px;gap:12px;align-items:center}
.nm{font-family:ui-monospace,Consolas,monospace;font-size:.82rem}
.badge{display:inline-block;font-size:.68rem;padding:1px 8px;border-radius:8px;margin-left:6px}
.ok{background:#e3f2e6;color:#2c7a3f}.pending{background:#eef1f5;color:#8a97a6}
.running{background:#e2ecf8;color:#1f5fa8}.failed{background:#fdeaea;color:#b03a3a}
.bar{height:8px;background:#e8edf3;border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;background:#4a7dbd;border-radius:4px;transition:width .8s}
.meta{font-size:.74rem;color:#7b8a9a;text-align:right;font-variant-numeric:tabular-nums}
.rit{color:#b03a3a}
#ts{color:#4a7dbd}
</style></head><body>
<h1>BIRD-dev 基线评测 · 自建端点 qwen</h1>
<div class=sub>run: bird-dev-baseline-selfhost ｜ 每 10 秒自动刷新 ｜ 最近更新 <b id=ts>--</b></div>
<div class=grid>
<div class=card><span>已完成阶段</span><b id=s-done>-</b></div>
<div class=card><span>生成调用 / 思考失控</span><b id=s-calls>-</b></div>
<div class=card><span>生成 token 累计</span><b id=s-tok>-</b></div>
<div class=card><span>向量 token 累计</span><b id=s-etok>-</b></div>
</div>
<div id=stages></div>
<script>
async function tick(){try{
 const r=await fetch('/api/status');const d=await r.json();
 document.getElementById('ts').textContent=new Date().toLocaleTimeString();
 const done=d.stages.filter(s=>s.state==='ok').length;
 document.getElementById('s-done').textContent=done+' / '+d.stages.length;
 document.getElementById('s-calls').textContent=d.usage.chat_calls+' / '+d.usage.at_cap;
 document.getElementById('s-tok').textContent=(d.usage.chat_tokens/1e4).toFixed(1)+' 万';
 document.getElementById('s-etok').textContent=(d.usage.embed_tokens/1e4).toFixed(2)+' 万';
 document.getElementById('stages').innerHTML=d.stages.map(s=>{
  const zh={ok:'完成',pending:'排队',running:'进行中',failed:'失败'}[s.state];
  const pct=s.state==='ok'?100:(s.prog?s.prog.pct:0);
  const pc=s.prog?s.prog.done+'/'+s.prog.total:'';
  let mt=[];if(s.wall!=null)mt.push(s.wall.toFixed(0)+'s');
  if(s.calls)mt.push(s.calls+' 次调用 '+Math.round(s.total_tokens/1000)+'k tok');
  const cls=s.state==='failed'?'meta rit':'meta';
  return '<div class=stage><div class=nm>'+s.name+'<span class="badge '+s.state+'">'+zh+'</span></div>' +
   '<div class=bar><i style=width:'+pct+'%></i></div>' +
   '<div class='+cls+'>'+(pc?pc+' ｜ ':'')+mt.join(' ｜ ')+'</div></div>';}).join('');
}catch(e){}}
tick();setInterval(tick,10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/status"):
            body = json.dumps(scan_run(RUN), ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
            code = 200
        elif self.path in ("/", "/index.html"):
            body, ctype, code = PAGE.encode("utf-8"), "text/html; charset=utf-8", 200
        else:
            body, ctype, code = b"not found", "text/plain", 404
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    global RUN
    ap.add_argument("--run", default="workspace/runs/bird-dev-baseline-selfhost")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    RUN = ROOT / args.run if not Path(args.run).is_absolute() else Path(args.run)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"dashboard on http://127.0.0.1:{args.port} watching {RUN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
