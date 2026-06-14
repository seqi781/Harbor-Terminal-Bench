"""
把 api_trace.jsonl 转换成网页，通过 HTTP 服务器在浏览器里查看。

用法：
    # 不带参数：扫描整个 jobs/ 下所有 job 的所有任务（默认，列表自动刷新）
    python trace_viewer.py
    # 指定某个 job 目录：只看该 job 下的任务
    python trace_viewer.py jobs/2026-06-13__12-40-55
    # 单个 trace
    python trace_viewer.py jobs/.../agent/api_trace.jsonl

启动后访问 http://localhost:9080 查看。
注意：若在 SSH 远程/VSCode-Remote 上运行，需在本地把 9080 端口转发过来
（VSCode 的 PORTS 面板 → Forward a Port → 9080）。
Ctrl+C 停止。
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from html import escape
from urllib.parse import urlparse, parse_qs


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0f1117; color: #e2e8f0; }

.page { max-width: 960px; margin: 0 auto; padding: 24px 16px; }

h1 { font-size: 1.4rem; font-weight: 600; color: #7dd3fc; margin-bottom: 4px; }
.subtitle { font-size: 0.8rem; color: #64748b; margin-bottom: 24px; }

.init-card {
    background: #1e293b; border: 1px solid #334155; border-radius: 8px;
    padding: 16px; margin-bottom: 24px;
}
.init-card .label { font-size: 0.7rem; color: #94a3b8; text-transform: uppercase; letter-spacing: .05em; margin-bottom: 4px; }
.init-card .task-text { font-size: 0.9rem; color: #e2e8f0; line-height: 1.6; white-space: pre-wrap; }
.init-card .meta { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; font-size: 0.78rem; color: #64748b; }
.init-card .meta span { background: #0f1117; border: 1px solid #334155; border-radius: 4px; padding: 2px 8px; }

.done-card {
    background: #1e293b; border: 1px solid #334155; border-radius: 8px;
    padding: 14px 16px; margin-top: 24px; display: flex; gap: 24px; flex-wrap: wrap;
}
.done-card .stat { font-size: 0.8rem; color: #94a3b8; }
.done-card .stat strong { display: block; font-size: 1.1rem; color: #7dd3fc; font-weight: 700; }

.turn { margin-bottom: 20px; }
.turn-header {
    font-size: 0.72rem; font-weight: 700; color: #334155;
    text-transform: uppercase; letter-spacing: .08em;
    margin-bottom: 8px; padding-left: 2px;
    border-top: 1px solid #1e293b; padding-top: 16px;
}

.bubble { border-radius: 8px; margin-bottom: 8px; overflow: hidden; border: 1px solid #1e293b; }
.bubble-send  { border-color: #1e40af40; }
.bubble-send  .bubble-head { background: #1e3a5f; color: #93c5fd; }
.bubble-recv  { border-color: #15803d40; }
.bubble-recv  .bubble-head { background: #14532d; color: #86efac; }
.bubble-tool-ok  { border-color: #b4560040; }
.bubble-tool-ok  .bubble-head { background: #431407; color: #fb923c; }
.bubble-tool-err { border-color: #99000040; }
.bubble-tool-err .bubble-head { background: #450a0a; color: #fca5a5; }

.bubble-head {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; font-size: 0.75rem; font-weight: 600;
    cursor: pointer; user-select: none;
}
.bubble-head .arrow { margin-left: auto; transition: transform .2s; font-size: 0.6rem; }
.bubble-head.open .arrow { transform: rotate(90deg); }

.bubble-body { background: #0f1117; padding: 12px 14px; display: none; }
.bubble-body.open { display: block; }

.section { margin-bottom: 12px; }
.section:last-child { margin-bottom: 0; }
.section-label {
    font-size: 0.65rem; font-weight: 700; color: #64748b;
    text-transform: uppercase; letter-spacing: .06em; margin-bottom: 5px;
}

pre {
    background: #1e293b; border: 1px solid #334155; border-radius: 6px;
    padding: 10px 12px; font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.78rem; line-height: 1.55; white-space: pre-wrap;
    word-break: break-all; color: #cbd5e1;
}

.thinking-block {
    background: #1a1a2e; border-left: 3px solid #6366f1;
    border-radius: 0 6px 6px 0; padding: 10px 12px;
    font-size: 0.78rem; line-height: 1.6; color: #a5b4fc;
    white-space: pre-wrap; font-style: italic;
}

.msg-list { display: flex; flex-direction: column; gap: 5px; }
.msg-item { border-radius: 5px; overflow: hidden; border: 1px solid #334155; }
.msg-item-head {
    display: flex; gap: 8px; align-items: center;
    padding: 5px 10px; font-size: 0.72rem; font-weight: 600;
    cursor: pointer; user-select: none;
}
.msg-item-body { background: #0f1117; padding: 8px 10px; display: none; }
.msg-item-body.open { display: block; }

.role-system    { background: #292524; color: #d6d3d1; }
.role-user      { background: #1c1917; color: #a8a29e; }
.role-assistant { background: #1e1b4b; color: #a5b4fc; }
.role-tool      { background: #14532d; color: #86efac; }

.badge-ok  { background: #15803d; color: #fff; padding: 1px 7px; border-radius: 99px; font-size: 0.65rem; }
.badge-err { background: #b91c1c; color: #fff; padding: 1px 7px; border-radius: 99px; font-size: 0.65rem; }

.tokens { font-size: 0.7rem; color: #475569; margin-left: auto; }
"""

JS = """
function toggle(el) {
    const head = el.closest('.bubble-head') || el.closest('.msg-item-head');
    if (!head) return;
    head.classList.toggle('open');
    const body = head.nextElementSibling;
    if (body) body.classList.toggle('open');
}
// 默认展开 recv 和 tool
document.querySelectorAll('.bubble-recv .bubble-head, .bubble-tool-ok .bubble-head, .bubble-tool-err .bubble-head')
    .forEach(h => { h.classList.add('open'); h.nextElementSibling.classList.add('open'); });
"""


def e(s):
    return escape(str(s or ""))


def pre(s):
    return f"<pre>{e(s)}</pre>"


def bubble(cls, icon, title, badge, body_html):
    return f"""
<div class="bubble {cls}">
  <div class="bubble-head" onclick="toggle(this)">
    {icon}&nbsp;{e(title)}&nbsp;{badge}
    <span class="arrow">▶</span>
  </div>
  <div class="bubble-body">{body_html}</div>
</div>"""


# ── SEND ────────────────────────────────────────────────────────────────────

def render_send(ev):
    msgs = ev.get("messages", [])
    items = []
    for m in msgs:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        tool_calls = m.get("tool_calls")
        summary = (content or "")[:80] + ("…" if len(content or "") > 80 else "")
        if tool_calls:
            summary += f"  [tool_calls ×{len(tool_calls)}]"
        full = json.dumps(m, indent=2, ensure_ascii=False)
        items.append(f"""
<div class="msg-item">
  <div class="msg-item-head role-{e(role)}" onclick="toggle(this)">
    <b>{e(role)}</b>
    <span style="color:#475569;font-weight:400">{e(summary)}</span>
    <span class="arrow" style="margin-left:auto;font-size:.6rem">▶</span>
  </div>
  <div class="msg-item-body">{pre(full)}</div>
</div>""")

    body = f"""
<div class="section">
  <div class="section-label">发送给 LLM 的完整消息（{len(msgs)} 条）</div>
  <div class="msg-list">{"".join(items)}</div>
</div>"""
    return bubble("bubble-send", "→", f"SEND  turn {ev['turn']}", "", body)


# ── RECV ────────────────────────────────────────────────────────────────────

def render_recv(ev):
    parts = []

    thinking = ev.get("thinking") or ""
    if thinking:
        parts.append(f"""
<div class="section">
  <div class="section-label">🧠 Thinking ({len(thinking):,} chars)</div>
  <div class="thinking-block">{e(thinking)}</div>
</div>""")

    content = ev.get("content") or ""
    if content:
        parts.append(f'<div class="section"><div class="section-label">💬 Content</div>{pre(content)}</div>')

    tool_calls = ev.get("tool_calls") or []
    if tool_calls:
        tc_json = json.dumps(tool_calls, indent=2, ensure_ascii=False)
        parts.append(f'<div class="section"><div class="section-label">🔧 Tool Calls ({len(tool_calls)} 个)</div>{pre(tc_json)}</div>')

    usage = ev.get("usage") or {}
    in_tok  = usage.get("prompt_tokens", 0)
    out_tok = usage.get("completion_tokens", 0)
    r_tok   = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    c_tok   = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    badge = f'<span class="tokens">in {in_tok} · out {out_tok} · think {r_tok} · cache {c_tok}</span>'

    return bubble("bubble-recv", "←", f"RECV  turn {ev['turn']}", badge, "".join(parts))


# ── TOOL ────────────────────────────────────────────────────────────────────

def render_tool(ev):
    rc     = ev.get("return_code", 0)
    cmd    = ev.get("command", "")
    stdout = ev.get("stdout", "")
    stderr = ev.get("stderr", "")
    ok     = rc == 0

    parts = [f'<div class="section"><div class="section-label">$ 命令</div>{pre(cmd)}</div>']
    if stdout:
        parts.append(f'<div class="section"><div class="section-label">stdout</div>{pre(stdout)}</div>')
    if stderr:
        parts.append(f'<div class="section"><div class="section-label">stderr</div>{pre(stderr)}</div>')

    cls   = "bubble-tool-ok" if ok else "bubble-tool-err"
    badge = f'<span class="{"badge-ok" if ok else "badge-err"}">exit {rc}</span>'
    title = f"TOOL  turn {ev['turn']}  {cmd.replace(chr(10), ' ')[:50]}"
    return bubble(cls, "⚙", title, badge, "".join(parts))


# ── DONE ────────────────────────────────────────────────────────────────────

def render_done(ev):
    turns   = ev.get("total_turns", "?")
    in_tok  = ev.get("total_input_tokens", 0)
    out_tok = ev.get("total_output_tokens", 0)
    return f"""
<div class="done-card">
  <div class="stat"><strong>{turns}</strong> 轮</div>
  <div class="stat"><strong>{in_tok:,}</strong> input tokens</div>
  <div class="stat"><strong>{out_tok:,}</strong> output tokens</div>
  <div class="stat"><strong>{in_tok + out_tok:,}</strong> 合计</div>
</div>"""


# ── 主流程 ───────────────────────────────────────────────────────────────────

def build_html(events, source_path, back_to_index=False):
    from collections import defaultdict

    init_ev  = next((e for e in events if e["event"] == "init"), {})
    done_ev  = next((e for e in events if e["event"] == "done"), {})
    loop_evs = [e for e in events if e["event"] in ("send", "recv", "tool")]

    turns = defaultdict(list)
    for ev in loop_evs:
        turns[ev["turn"]].append(ev)

    task_text = init_ev.get("task", "")
    model     = init_ev.get("model", "")
    max_t     = init_ev.get("max_turns", "")

    init_html = f"""
<div class="init-card">
  <div class="label">任务描述</div>
  <div class="task-text">{e(task_text)}</div>
  <div class="meta">
    <span>model: {e(model)}</span>
    <span>max_turns: {e(max_t)}</span>
    <span>{e(source_path)}</span>
  </div>
</div>"""

    turn_blocks = []
    for turn_num in sorted(turns.keys()):
        blocks = []
        for ev in turns[turn_num]:
            if ev["event"] == "send":
                blocks.append(render_send(ev))
            elif ev["event"] == "recv":
                blocks.append(render_recv(ev))
            elif ev["event"] == "tool":
                blocks.append(render_tool(ev))
        turn_blocks.append(f"""
<div class="turn">
  <div class="turn-header">Turn {turn_num}</div>
  {"".join(blocks)}
</div>""")

    done_html = render_done(done_ev) if done_ev else ""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Trace Viewer</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">
  {'<a href="/" style="color:#7dd3fc;font-size:0.8rem;text-decoration:none">← 返回任务列表</a>' if back_to_index else ''}
  <h1>Agent Trace Viewer</h1>
  <div class="subtitle">{escape(str(source_path))}</div>
  {init_html}
  {"".join(turn_blocks)}
  {done_html}
</div>
<script>{JS}</script>
</body>
</html>"""


PORT = 9080


def load_events(src: Path) -> list:
    events = []
    with src.open() as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


# ── job 目录模式 ─────────────────────────────────────────────────────────────

def find_traces(root: Path) -> list:
    """
    找到 root 下所有任务的 api_trace.jsonl。
    - root = jobs/ 根目录：匹配 <job>/<trial>/agent/api_trace.jsonl（两层）
    - root = 单个 job 目录：匹配 <trial>/agent/api_trace.jsonl（一层）
    两种都试，返回去重后的路径列表。
    """
    traces = set(root.glob("*/*/agent/api_trace.jsonl"))
    traces |= set(root.glob("*/agent/api_trace.jsonl"))
    return sorted(traces)


def scan_job_dir(root: Path) -> list:
    """扫描 root 下每个任务的摘要。每次请求重扫，支持边跑边实时刷新。"""
    entries = []
    for trace_path in find_traces(root):
        trial_dir = trace_path.parent.parent          # <task>__<id>/
        trial_name = trial_dir.name
        rel = trial_dir.relative_to(root).as_posix()  # 详情页定位用（跨 job 唯一）
        job_name = trial_dir.parent.name if trial_dir.parent != root else ""
        try:
            mtime = trace_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        reward = in_tok = out_tok = turns = None

        # 优先用 result.json（reward + token 最权威）
        result_path = trial_dir / "result.json"
        if result_path.exists():
            try:
                r = json.loads(result_path.read_text())
                reward = (r.get("verifier_result") or {}).get("rewards", {}).get("reward")
                ar = r.get("agent_result") or {}
                in_tok = ar.get("n_input_tokens")
                out_tok = ar.get("n_output_tokens")
            except (json.JSONDecodeError, OSError):
                pass

        # reward 回退：verifier/reward.txt
        if reward is None:
            reward_txt = trial_dir / "verifier" / "reward.txt"
            if reward_txt.exists():
                try:
                    reward = float(reward_txt.read_text().strip())
                except (ValueError, OSError):
                    pass

        # turns / token 回退：trace 的 done 事件
        if turns is None or in_tok is None:
            try:
                evs = load_events(trace_path)
                done = next((e for e in evs if e.get("event") == "done"), None)
                if done:
                    turns = done.get("total_turns")
                    in_tok = in_tok if in_tok is not None else done.get("total_input_tokens")
                    out_tok = out_tok if out_tok is not None else done.get("total_output_tokens")
                elif turns is None:
                    turns = sum(1 for e in evs if e.get("event") == "send")
            except (json.JSONDecodeError, OSError):
                pass

        if reward is None:
            status = "pending"
        elif reward >= 1.0:
            status = "pass"
        else:
            status = "fail"

        entries.append({
            "trial_name": trial_name,
            "rel": rel,
            "job": job_name,
            "mtime": mtime,
            "trace_path": trace_path,
            "reward": reward,
            "in_tok": in_tok,
            "out_tok": out_tok,
            "turns": turns,
            "status": status,
        })
    return entries


def _ago(ts: float, now: float) -> str:
    """把时间戳渲染成「几秒/分钟/小时前」。"""
    if not ts:
        return "—"
    d = max(0, int(now - ts))
    if d < 60:
        return f"{d}s 前"
    if d < 3600:
        return f"{d // 60}m 前"
    if d < 86400:
        return f"{d // 3600}h 前"
    return f"{d // 86400}d 前"


def build_index_html(job_dir: Path, entries: list, *, refresh_sec: int = 8,
                     multi_job: bool = True) -> str:
    import time
    now = time.time()

    total = len(entries)
    passed = sum(1 for e in entries if e["status"] == "pass")
    failed = sum(1 for e in entries if e["status"] == "fail")
    pending = sum(1 for e in entries if e["status"] == "pending")
    rate = f"{passed / total * 100:.1f}%" if total else "—"

    def fmt(n):
        return f"{n:,}" if isinstance(n, int) else "—"

    def task_row(e):
        st = e["status"]
        badge_cls = {"pass": "badge-ok", "fail": "badge-err", "pending": "badge-pending"}[st]
        reward_txt = "—" if e["reward"] is None else f"{e['reward']:g}"
        return f"""
<tr class="idx-row idx-{st}" onclick="location.href='/?t={escape(e['rel'])}'">
  <td><span class="{badge_cls}">{escape(reward_txt)}</span></td>
  <td class="idx-name">{escape(e['trial_name'])}</td>
  <td class="idx-num">{fmt(e['turns'])}</td>
  <td class="idx-num">{fmt(e['in_tok'])}</td>
  <td class="idx-num">{fmt(e['out_tok'])}</td>
  <td class="idx-num idx-ago">{escape(_ago(e['mtime'], now))}</td>
</tr>"""

    table_head = ('<tr><th>reward</th><th>任务</th><th>轮数</th>'
                  '<th>in tok</th><th>out tok</th><th>更新</th></tr>')
    order = {"fail": 0, "pending": 1, "pass": 2}

    # 按 job（文件结构）分组。多 job：每个 job 一个折叠分组；单 job：一张表。
    sections = []
    if multi_job:
        jobs = {}
        for e in entries:
            jobs.setdefault(e["job"], []).append(e)
        # job 目录名是时间戳，最新的排上面
        for job in sorted(jobs, reverse=True):
            items = jobs[job]
            j_pass = sum(1 for e in items if e["status"] == "pass")
            j_fail = sum(1 for e in items if e["status"] == "fail")
            j_pend = sum(1 for e in items if e["status"] == "pending")
            j_last = max((e["mtime"] for e in items), default=0)
            # 组内：失败优先，便于排查
            rows = "".join(task_row(e) for e in
                           sorted(items, key=lambda e: (order[e["status"]], e["trial_name"])))
            sections.append(f"""
<details class="job-group" open>
  <summary class="job-head">
    <span class="job-name">{escape(job)}/</span>
    <span class="job-stat">{len(items)} 任务</span>
    <span class="job-stat ok">{j_pass} 通过</span>
    <span class="job-stat err">{j_fail} 失败</span>
    {f'<span class="job-stat pend">{j_pend} 未完成</span>' if j_pend else ''}
    <span class="job-stat ago">更新 {escape(_ago(j_last, now))}</span>
  </summary>
  <table class="idx">{table_head}{rows}</table>
</details>""")
    else:
        rows = "".join(task_row(e) for e in
                       sorted(entries, key=lambda e: (order[e["status"]], e["trial_name"])))
        sections.append(f'<table class="idx">{table_head}{rows}</table>')

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh_sec}">
<title>Job Trace Index</title>
<style>{CSS}
table.idx {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
table.idx th {{ text-align: left; font-size: 0.7rem; color: #64748b; text-transform: uppercase;
  letter-spacing: .05em; padding: 6px 10px; border-bottom: 1px solid #334155; }}
.idx-row {{ cursor: pointer; border-bottom: 1px solid #1e293b; }}
.idx-row:hover {{ background: #1e293b; }}
.idx-row td {{ padding: 8px 10px; font-size: 0.82rem; }}
.idx-name {{ font-family: 'JetBrains Mono', monospace; color: #cbd5e1; }}
.idx-job {{ font-family: 'JetBrains Mono', monospace; color: #64748b; font-size: 0.75rem; }}
.idx-num {{ text-align: right; color: #94a3b8; font-variant-numeric: tabular-nums; }}
.idx-ago {{ color: #475569; }}
.idx-fail .idx-name {{ color: #fca5a5; }}
.badge-pending {{ background: #475569; color: #fff; padding: 1px 7px; border-radius: 99px; font-size: 0.7rem; }}
.job-group {{ margin-top: 18px; border: 1px solid #1e293b; border-radius: 8px; overflow: hidden; }}
.job-head {{ display: flex; align-items: center; gap: 12px; cursor: pointer;
  padding: 10px 14px; background: #1e293b; user-select: none; font-size: 0.8rem; }}
.job-head::-webkit-details-marker {{ display: none; }}
.job-name {{ font-family: 'JetBrains Mono', monospace; color: #7dd3fc; font-weight: 600; }}
.job-stat {{ color: #94a3b8; font-size: 0.75rem; }}
.job-stat.ok {{ color: #86efac; }}
.job-stat.err {{ color: #fca5a5; }}
.job-stat.pend {{ color: #cbd5e1; }}
.job-stat.ago {{ margin-left: auto; color: #475569; }}
.job-group table.idx {{ margin-top: 0; }}
</style>
</head>
<body>
<div class="page">
  <h1>Job Trace Index</h1>
  <div class="subtitle">{escape(str(job_dir))} · 每 {refresh_sec}s 自动刷新 · {time.strftime('%H:%M:%S', time.localtime(now))} 扫描</div>
  <div class="done-card">
    <div class="stat"><strong>{total}</strong> 任务</div>
    <div class="stat"><strong>{passed}</strong> 通过</div>
    <div class="stat"><strong>{failed}</strong> 失败</div>
    <div class="stat"><strong>{pending}</strong> 未完成</div>
    <div class="stat"><strong>{rate}</strong> 通过率</div>
  </div>
  {"".join(sections)}
</div>
</body>
</html>"""


def main():
    # 不带参数 → 默认扫描整个 jobs/ 根目录
    src = Path(sys.argv[1]) if len(sys.argv) >= 2 else Path("jobs")
    if not src.exists():
        print(f"路径不存在: {src}")
        sys.exit(1)

    is_dir = src.is_dir()
    # 多 job 模式：root 下存在两层结构（<job>/<trial>/agent/...）
    multi_job = is_dir and bool(list(src.glob("*/*/agent/api_trace.jsonl")))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, html: str, code: int = 200):
            data = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # 每次请求都重新读取，方便边跑边刷新
            if not is_dir:
                self._send(build_html(load_events(src), str(src)))
                return

            # 目录模式：/ → 索引页；/?t=<rel> → 详情页（rel 可能含 job 层）
            query = parse_qs(urlparse(self.path).query)
            trial = (query.get("t") or [None])[0]
            if not trial:
                self._send(build_index_html(src, scan_job_dir(src), multi_job=multi_job))
                return

            trace_path = src / trial / "agent" / "api_trace.jsonl"
            if not trace_path.exists():
                self._send(
                    f"<p style='font-family:sans-serif'>找不到 trace: {escape(trial)}"
                    f"<br><a href='/'>← 返回列表</a></p>", code=404)
                return
            self._send(build_html(load_events(trace_path), trace_path, back_to_index=True))

        def log_message(self, *_a, **_k):
            pass

    HTTPServer.allow_reuse_address = True
    try:
        server = HTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as exc:
        print(f"无法绑定端口 {PORT}：{exc}")
        print(f"  可能已有旧进程占用，先停掉它：  fuser -k {PORT}/tcp")
        sys.exit(1)
    print(f"✓ 服务已启动：http://localhost:{PORT}")
    print(f"  {'目录' if is_dir else '文件'}：{src}")
    print("  Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
