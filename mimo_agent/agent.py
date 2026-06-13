import json
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .config import MAX_OUTPUT_CHARS, MAX_TURNS, MODEL
from .key_pool import KeyPool

load_dotenv()


def _to_serializable(obj):
    """把 OpenAI 对象递归转成普通 dict，方便 JSON 序列化。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_to_serializable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    return obj

# ── 工具定义 ────────────────────────────────────────────────────────────────
# 这段 JSON 是"说明书"，告诉 LLM 它可以调用什么函数。
# LLM 自己不会执行命令——它只负责决定"该调用哪个工具、参数是什么"。
# 真正执行命令的是下面 run() 里的 environment.exec()。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在 bash 终端里执行一条命令，返回输出结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 bash 命令，例如 ls -la 或 python3 script.py",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": (
                "覆盖式更新你的工作记忆笔记。记下绝不能忘的关键事实："
                "环境信息、解题文件的确切路径以及如何运行/导入它"
                "（例如：run.py 在 /app，测试需 cd /app 或 PYTHONPATH=/app）、"
                "要求的函数签名、已经验证通过的内容。保持简短、结构化。"
                "只在每个阶段结束、即将进入下一阶段时调用一次，不要每轮或阶段中途调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "notes": {
                        "type": "string",
                        "description": "新的完整记忆内容，会整体替换旧的。",
                    }
                },
                "required": ["notes"],
            },
        },
    },
]

SYSTEM_PROMPT = """\
You are a terminal agent inside a Docker container. Complete the given task by running shell commands.

Work in three phases. Do NOT skip ahead. Begin every response by stating which phase
you are in using its marker tag below, so it is always clear where you are.

<explore environment>
PHASE A — RECON (explore first):
Before writing ANY solution code, investigate the environment with read-only commands.
You MUST find out:
  - the current directory and its files
  - the runtime version (e.g. python3 --version)
  - whether every module/package you intend to use can actually be imported
  - which existing files you need to create or edit, and their exact required paths
  - how the task will be checked: look for test files, the exact function signature,
    and any expected output paths the task names
RULE: do not write solution code until recon has confirmed your approach will work
(dependencies importable, signature and file path correct). Most import / path errors
come from skipping this step.

<implement>
PHASE B — IMPLEMENT (smallest steps):
Make the smallest change that moves forward, then immediately confirm it loads/imports
before continuing. Do not write the whole solution in one shot.

<verify>
PHASE C — VERIFY (prove it with assertions + exit codes, never with a printed "ok"):
Before you declare the task done you MUST:
  - re-read the task's exact requirements (signature, file path, behavior)
  - WRITE AND RUN a test that ASSERTS the required behavior. Use `assert` (or raise on
    mismatch) so that a WRONG result makes the program crash with a NON-ZERO exit code.
    For a behavioral requirement (e.g. a concurrency limit), assert the behavior really
    holds — never accept "it imports" as proof it works.
  - NEVER write code that unconditionally prints "ok"/"PASS"/"success". A printed
    success word proves NOTHING: it prints the same whether the code is right or wrong.
Only after your assertion-based test exits 0 may you stop calling tools and report.

How to think and act:
  - Plan only the next 1-3 actions. Do not design the entire solution in your head up
    front. After each command's output, re-plan based on what you actually saw.
  - Stay strictly on the task's real requirements. Do not test or implement behaviors
    the task did not ask for.
  - When you have several INDEPENDENT commands (especially during recon), issue them as
    multiple tool calls in a SINGLE response instead of one at a time.
  - JUDGE EVERY COMMAND BY ITS EXIT CODE, NOT BY WORDS IT PRINTED. Each tool result
    starts with `[exit code: N]`. N==0 means success; N!=0 means it FAILED — never call
    anything done while its check exits non-zero. A script that prints "ok" can print
    "ok" even when it is broken; an exit code cannot lie. When checking, prefer commands
    that fail loudly (`assert`, `set -e`, raising) over ones that print a status word.

Working memory (so you never re-derive what you already learned):
  - A "## 工作记忆" note is pinned at the very BOTTOM of the conversation — the one place
    that never scrolls away. Re-read it before each action.
  - Update it EXACTLY ONCE PER PHASE: right when you finish a phase and are about to move
    on to the next one, call update_memory a single time to overwrite the note. Do NOT
    update it every turn or in the middle of a phase. Each update carries forward the key
    facts: exact file paths and HOW to run / import the solution (e.g. solution is
    /app/run.py, tests need `cd /app` or `PYTHONPATH=/app`), runtime version, the required
    function signature, and what you have already verified. Keep it short and structured —
    it replaces the old note entirely, so carry forward what still matters.
"""


class MiMoAgent(BaseAgent):
    """Harbor terminal-bench agent powered by the MiMo API."""

    # ── Harbor 要求的元信息 ──────────────────────────────────────────────────

    @staticmethod
    def name() -> str:
        return "mimo-agent"

    def version(self) -> str:
        return "0.1.0"

    # ── Harbor 在任务开始前调用 setup() ─────────────────────────────────────
    # 可以在这里安装依赖工具。我们暂时不需要，留空即可。

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    # ── Harbor 调用 run() 来执行任务 ─────────────────────────────────────────
    # instruction = 任务描述文字
    # environment = 可以在 Docker 容器里执行命令的对象
    # context     = 用来记录 token 用量、花费等统计信息

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # 1. 拿一个可用的 API key，创建 LLM 客户端
        key_pool = KeyPool.from_env(MODEL)
        key = await key_pool.acquire()
        client = AsyncOpenAI(api_key=key, base_url="https://api.xiaomimimo.com/v1")

        # 2. 消息列表 = Agent 的"记忆"
        #    每一轮都往里追加，让 LLM 能看到完整的对话历史
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]

        total_input = total_output = 0
        last_response = None
        # 上一轮是否有命令失败（rc != 0）——用作"遇到难题"信号，决定本轮是否开思考
        prev_turn_failed = False
        # 工作记忆：模型通过 update_memory 覆盖更新；每轮注入到对话最底部，永不滚走。
        memory_text = ""

        # 日志文件：每一轮追加一行 JSON，完整记录对话过程
        trace_path = Path(self.logs_dir) / "api_trace.jsonl"

        def log(event: str, data: dict):
            with trace_path.open("a") as f:
                f.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")

        # 记录任务开始
        log("init", {"task": instruction, "model": MODEL, "max_turns": MAX_TURNS})

        # 3. Agent 循环：最多跑 MAX_TURNS 轮
        for turn in range(MAX_TURNS):
            # 思考开关：第一轮（拿到问题、做总体规划）开思考；之后默认关掉、快速行动，
            # 复用历史里那份首轮思考；只有当上一轮有命令失败（遇到难题）时再重新开思考。
            thinking_on = (turn == 0) or prev_turn_failed
            self.logger.info(
                f"[turn {turn + 1}] 发送消息给 LLM ... (thinking={'on' if thinking_on else 'off'})"
            )

            # ── Think：让 LLM 决定下一步做什么 ─────────────────────────────
            # 把当前工作记忆注入到对话最底部（永不滚走）。它不进 messages 历史，
            # 每轮用最新内容重建，保证始终只有一份、且在最后。
            request_messages = list(messages)
            if memory_text:
                request_messages.append({
                    "role": "system",
                    "content": f"## 工作记忆（你自己维护，保持最新；行动前先看这里）\n{memory_text}",
                })

            # 记录本轮真正发送给 LLM 的完整 messages（含历史 + 底部记忆）
            log("send", {"turn": turn + 1, "thinking": thinking_on, "messages": _to_serializable(request_messages)})

            last_response = await client.chat.completions.create(
                model=MODEL,
                messages=request_messages,
                tools=TOOLS,
                parallel_tool_calls=True,
                extra_body={"thinking": {"type": "enabled" if thinking_on else "disabled"}},
            )

            msg = last_response.choices[0].message

            if last_response.usage:
                total_input += last_response.usage.prompt_tokens or 0
                total_output += last_response.usage.completion_tokens or 0

            # 记录 LLM 的完整回复：思考过程、文字内容、工具调用、token 用量
            log("recv", {
                "turn": turn + 1,
                "thinking": getattr(msg, "reasoning_content", None),
                "content": msg.content,
                "tool_calls": _to_serializable(msg.tool_calls) if msg.tool_calls else None,
                "usage": _to_serializable(last_response.usage),
            })


            # 把 LLM 的回复加入历史（必须加，否则下一轮 LLM 会"失忆"）
            messages.append(msg)

            # ── Act：检查 LLM 是否要调用工具 ─────────────────────────────
            if not msg.tool_calls:
                self.logger.info(f"[turn {turn + 1}] 任务完成，LLM 停止调用工具")
                break

            # ── Observe：执行工具调用，把结果喂回给 LLM ──────────────────
            # 本轮可能有多个 tool_call（并行工具调用）；顺序执行，记录是否有失败。
            turn_failed = False
            for tool_call in msg.tool_calls:
                args = json.loads(tool_call.function.arguments)

                # update_memory：不进容器，只覆盖工作记忆，回一句确认即可。
                if tool_call.function.name == "update_memory":
                    memory_text = args.get("notes", "")
                    self.logger.info(f"[turn {turn + 1}] 更新工作记忆 ({len(memory_text)} 字)")
                    log("memory", {"turn": turn + 1, "notes": memory_text})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "(工作记忆已保存)",
                    })
                    continue

                command = args["command"]
                self.logger.info(f"[turn {turn + 1}] 执行命令: {command}")

                # 在 Docker 容器里执行命令，拿到 stdout / stderr / return_code
                result = await environment.exec(command, timeout_sec=120)

                stdout = result.stdout or ""
                stderr = result.stderr or ""
                # 合并后截断，保留末尾（末尾通常含最重要的错误信息）
                combined = stdout + stderr or "(no output)"
                if len(combined) > MAX_OUTPUT_CHARS:
                    combined = "...(truncated)\n" + combined[-MAX_OUTPUT_CHARS:]
                # 把退出码放在最前面交给 LLM：这是唯一可信的成功/失败信号，
                # 不能靠脚本里 print 出来的 "ok"（对错都会照样打印）来判断。
                combined = f"[exit code: {result.return_code}]\n{combined}"

                self.logger.info(f"[turn {turn + 1}] exit={result.return_code}  output={combined[:200]}")

                if result.return_code != 0:
                    turn_failed = True

                # 记录工具执行的完整结果：命令、退出码、stdout、stderr
                log("tool", {
                    "turn": turn + 1,
                    "command": command,
                    "return_code": result.return_code,
                    "stdout": stdout[:MAX_OUTPUT_CHARS] if stdout else "",
                    "stderr": stderr[:MAX_OUTPUT_CHARS] if stderr else "",
                })

                # 把执行结果追加进 messages，LLM 下一轮才能看到
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": combined,
                })

            # 本轮有命令失败 → 下一轮重新开思考，集中处理这个难题
            prev_turn_failed = turn_failed

        # 记录任务结束和 token 汇总
        log("done", {
            "total_turns": turn + 1,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        })

        # 4. 填写统计信息，Harbor 用这些数据生成报告
        context.n_input_tokens = total_input
        context.n_output_tokens = total_output

        # 5. 记录 key 的消耗，释放占用
        usage = last_response.usage if last_response else None
        await key_pool.record(key, usage)
        await key_pool.release(key)
