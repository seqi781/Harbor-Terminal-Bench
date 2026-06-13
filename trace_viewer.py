"""
把 api_trace.jsonl 转换成网页，通过 HTTP 服务器在浏览器里查看。

用法：
    python trace_viewer.py <api_trace.jsonl 路径>
    python trace_viewer.py jobs/.../agent/api_trace.jsonl

启动后访问 http://localhost:9080 查看。
Ctrl+C 停止。
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from html import escape


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

def build_html(events, source_path):
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
  <h1>Agent Trace Viewer</h1>
  <div class="subtitle">api_trace.jsonl 可视化</div>
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


def main():
    if len(sys.argv) < 2:
        print("用法: python trace_viewer.py <api_trace.jsonl>")
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"文件不存在: {src}")
        sys.exit(1)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # 每次请求都重新读取文件，方便边跑边刷新
            events = load_events(src)
            html = build_html(events, str(src)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

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
    print(f"  文件：{src}")
    print("  Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
