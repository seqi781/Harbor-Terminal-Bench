import json
from pathlib import Path

import openai
from dotenv import load_dotenv
from openai import AsyncOpenAI

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .config import (
    IMPLEMENT_MAX_TURNS,
    MAX_OUTPUT_CHARS,
    MAX_TURNS,
    MODEL,
    RECON_MAX_TURNS,
    REVISE_AFTER_FAILS,
    VERIFY_MAX_TURNS,
)
from .key_pool import KeyPool

load_dotenv()

BASE_URL = "https://api.xiaomimimo.com/v1"


def _is_key_error(exc: Exception) -> bool:
    """判断异常是否是「这个 key 本身的问题」（认证 / 权限 / 余额），需要换 key。"""
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return True
    if getattr(exc, "status_code", None) in (401, 402, 403):
        return True
    msg = str(exc).lower()
    return any(w in msg for w in ("balance", "insufficient", "quota", "余额", "欠费"))


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
# 每个阶段给模型一套不同的工具：除了执行命令，还有一个「结束本阶段」的工具，
# 由 harness 接住、控制阶段切换（模型不再靠自觉分阶段）。
UPDATE_PLAN = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "写入/更新工作记忆里的计划——不结束当前阶段。两种时机调用："
            "①IMPLEMENT 开局：基于 RECON 的环境事实，把任务想透后写下 "
            "requirements（任务要求逐条清单：精确目标路径、函数/CLI 签名、期望的精确"
            "输出/数值、判定方式）和 plan（满足每条要求的有序步骤），动手前必须先写。"
            "②计划走不通时：连续失败后重写 plan，并在开头用一句话说明改了什么、为什么。"
            "这份记忆会钉在后续每一轮对话的最底部，覆盖式更新。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requirements": {"type": "string", "description": "任务要求逐条清单（含精确数值/路径/签名）；首次必填，修订计划时可省略"},
                "plan": {"type": "string", "description": "满足每条要求的有序步骤（修订时写新计划）"},
            },
            "required": ["plan"],
        },
    },
}

RUN_COMMAND = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "在 bash 终端里执行一条命令，返回输出（开头是 [exit code: N]）",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 bash 命令"},
            },
            "required": ["command"],
        },
    },
}

RUN_TEST = {
    "type": "function",
    "function": {
        "name": "run_test",
        "description": (
            "运行你的断言型测试命令（结果错就会非零退出）。这是验收专用工具——"
            "harness 用它的退出码作为是否允许提交的依据。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "运行断言测试的 bash 命令"},
            },
            "required": ["command"],
        },
    },
}

REPORT_FINDINGS = {
    "type": "function",
    "function": {
        "name": "report_findings",
        "description": (
            "结束 RECON 阶段。提交一份简短、结构化的勘察结论："
            "文件路径、运行时版本、可导入的依赖、要求的签名/输出、如何被校验、"
            "以及如何运行/导入解题文件（如 cd /app 或 PYTHONPATH=/app）。"
            "这份结论会被锁定并在后续阶段一直展示给你。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "findings": {"type": "string", "description": "结构化的勘察结论"},
            },
            "required": ["findings"],
        },
    },
}

FINISH_IMPLEMENTATION = {
    "type": "function",
    "function": {
        "name": "finish_implementation",
        "description": "结束 IMPLEMENT 阶段：解题文件已写好且能正常导入/加载时调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "实现了什么、文件在哪"},
            },
            "required": ["summary"],
        },
    },
}

SUBMIT = {
    "type": "function",
    "function": {
        "name": "submit",
        "description": (
            "提交并结束任务。只有在 run_test 已经退出码 0（断言测试通过）之后才允许调用，"
            "否则会被 harness 拒绝。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "做了什么、如何验证通过的"},
            },
            "required": ["summary"],
        },
    },
}


# ── 提示词：一段通用底座 + 每个阶段一小段 ───────────────────────────────────
BASE_PROMPT = """\
You are a terminal agent inside a Docker container. You solve the task by running shell
commands through tools.

Global rules:
- JUDGE EVERY COMMAND BY ITS EXIT CODE, shown as `[exit code: N]` at the start of each
  result. N==0 = success, N!=0 = failure. A printed "ok"/"PASS" proves NOTHING — it
  prints the same whether the code is right or wrong; only the exit code is trustworthy.
- When you have several INDEPENDENT commands, issue them as multiple tool calls in ONE
  response, not one at a time.
- Stay strictly on the task's real requirements; do not implement or test behaviors it
  did not ask for.

You work in three phases (RECON → IMPLEMENT → VERIFY) and the SYSTEM moves you between
them. Do what the current phase asks, then call its finishing tool.
A WORKING MEMORY (confirmed facts, requirements, plan, progress) is pinned to the BOTTOM of
every turn — it is your anchor. The plan is written AFTER you have explored (start of
IMPLEMENT) and can be REVISED via update_plan when it stops working. No matter how long the
conversation grows, keep satisfying EVERY item in the requirements; never lose the goal.
"""

RECON_PROMPT = """\
CURRENT PHASE: RECON — explore the environment. Do NOT write solution code yet.
Investigate with read-only commands and find out:
  - the working directory and its files; the runtime version
  - whether every package/module you intend to use actually imports
  - the exact path(s) of the file(s) you must create or edit
  - the exact required signature / output format
  - how the task is checked (test files, expected output paths)
BE FAST AND BATCHED: fire many checks as multiple run_command calls in ONE response. Aim
to finish in 1-2 turns.
When done, call report_findings(findings=...) with a short structured summary of all of
the above, INCLUDING how to run/import the solution (e.g. solution at /app/run.py, tests
need `cd /app` or PYTHONPATH=/app). These findings are LOCKED and shown to you in later
phases, so capture everything that matters.
"""

IMPLEMENT_PROMPT = """\
CURRENT PHASE: IMPLEMENT.
Your RECON findings are pinned below as working memory.

MANDATORY FIRST ACTION: Your very first response in this phase MUST be a call to
update_plan(requirements=..., plan=...). Do NOT call run_command first.
- requirements: precise list of what must be true (exact file paths, function signatures,
  expected output values, how it is verified).
- plan: ordered steps to implement the requirements.
Do this BEFORE writing any solution code.

Then execute the plan step by step. Write a complete, coherent solution — not one tiny edit
per turn. After writing, confirm it imports/loads (exit code 0).
If the plan stops working (you keep failing), STOP retrying the same way: rethink the root
cause and call update_plan again with a revised plan (say what changed and why).
When the solution file is written and loads cleanly, call finish_implementation(summary=...).
"""

VERIFY_PROMPT = """\
CURRENT PHASE: VERIFY.
Write a test that ASSERTS the required behavior so that a WRONG result CRASHES with a
non-zero exit code (use `assert` or raise on mismatch). Re-read the task's exact
requirements (signature, path, behavior) and make the test actually exercise them — for a
behavioral requirement (e.g. a concurrency limit) assert the behavior really holds, never
accept "it imports" as proof.
When the task NAMES a specific trigger or condition (e.g. KeyboardInterrupt / a signal / a
timeout / a specific input format), reproduce it the most REALISTIC way — for a real
interrupt, spawn the solution in a subprocess and send the actual signal (e.g. SIGINT),
do not substitute a convenient approximation like an in-process asyncio cancel. Also cover
boundary combinations the wording implies (e.g. more items than the limit, empty input).
Check against the EXACT expected values in your requirements memory — assert the precise
number / string / format the task names (e.g. an exact token count or output literal), not
just "a result was produced". If the task ships its OWN checker (e.g. /app/check_*.py,
files under /tests/, an expected-output file), run THAT and make sure every library it
imports is actually installed (pip install if missing). Only change what the task asks —
if it wants a specific edit, diff against the original to confirm you did not touch
anything else.
Run that test with the run_test tool (NOT run_command). Never write code that
unconditionally prints "ok".
You may call submit ONLY AFTER run_test has exited 0 — the system rejects submit until it
has seen a passing run_test.
"""


class MiMoAgent(BaseAgent):
    """Harbor terminal-bench agent powered by the MiMo API (harness-driven phases)."""

    @staticmethod
    def name() -> str:
        return "mimo-agent"

    def version(self) -> str:
        return "0.4.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # 1. key / client
        key_pool = KeyPool.from_env(MODEL)
        key = await key_pool.acquire()
        client = AsyncOpenAI(api_key=key, base_url=BASE_URL)

        async def call_llm(**kwargs):
            """调用 LLM；若当前 key 认证/余额失败，标记失败并换下一个 key 重试。"""
            nonlocal key, client
            while True:
                try:
                    return await client.chat.completions.create(**kwargs)
                except Exception as exc:
                    if not _is_key_error(exc):
                        raise
                    await key_pool.mark_failed(key, f"{type(exc).__name__}: {exc}"[:200])
                    self.logger.warning(f"key {key[:12]}… 失效（{exc}），换下一个 key")
                    key = await key_pool.acquire()  # 没 key 了会抛 NoAvailableKeyError
                    client = AsyncOpenAI(api_key=key, base_url=BASE_URL)

        # 2. 共享状态
        messages: list[dict] = [
            {"role": "system", "content": BASE_PROMPT},
            {"role": "user", "content": instruction},
        ]
        total_input = total_output = 0
        global_turn = 0          # 跨阶段连续编号，方便 trace_viewer 分组
        last_test_rc = None      # 最近一次 run_test 的退出码（VERIFY 门禁用）

        # 工作记忆：每轮钉在对话最底部，防止长对话里忘掉任务目标。
        mem = {
            "facts": "",          # RECON 确认的环境事实
            "requirements": "",   # IMPLEMENT 开局 update_plan 写入（精确路径/签名/期望输出/判定）
            "plan": "",           # IMPLEMENT 开局写入，失败时可经 update_plan 修订
            "progress": [],        # 每个阶段结束追加一条
        }

        def render_memory() -> str:
            if not any((mem["facts"], mem["requirements"], mem["plan"], mem["progress"])):
                return ""
            parts = ["## 工作记忆（每轮重发，始终最新——盯住任务要求，别跑偏）"]
            if mem["facts"]:
                parts += ["### 环境事实（RECON 确认）", mem["facts"]]
            if mem["requirements"]:
                parts += ["### 任务要求（必须逐条满足）", mem["requirements"]]
            if mem["plan"]:
                parts += ["### 计划（可随失败修订）", mem["plan"]]
            if mem["progress"]:
                parts += ["### 进度", "\n".join(f"- {p}" for p in mem["progress"])]
            return "\n".join(parts)

        trace_path = Path(self.logs_dir) / "api_trace.jsonl"

        def log(event: str, data: dict):
            with trace_path.open("a") as f:
                f.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")

        def append_tool(tool_call_id: str, content: str):
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

        async def exec_shell(command: str, kind: str, phase: str, turn: int):
            """在容器里执行命令，记 trace，返回 (退出码, 给 LLM 的内容)。"""
            self.logger.info(f"[{phase} t{turn}] {kind}: {command}")
            result = await environment.exec(command, timeout_sec=120)
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            combined = stdout + stderr or "(no output)"
            if len(combined) > MAX_OUTPUT_CHARS:
                combined = "...(truncated)\n" + combined[-MAX_OUTPUT_CHARS:]
            combined = f"[exit code: {result.return_code}]\n{combined}"
            log("tool", {
                "turn": turn, "phase": phase, "kind": kind, "command": command,
                "return_code": result.return_code,
                "stdout": stdout[:MAX_OUTPUT_CHARS] if stdout else "",
                "stderr": stderr[:MAX_OUTPUT_CHARS] if stderr else "",
            })
            return result.return_code, combined

        def verify_gate(_args):
            """严格门禁：必须见过一次 run_test 退出码 0 才放行 submit。"""
            if last_test_rc == 0:
                return True, ""
            return False, (
                "submit 被拒绝：还没看到你的断言测试通过。请用 run_test 运行一个"
                "「结果错就会非零退出」的断言测试，看到 [exit code: 0] 之后再 submit。"
            )

        async def run_phase(phase, prompt, tools, exit_tool, max_turns, *,
                            think_first=True, gate=None,
                            revise_after=REVISE_AFTER_FAILS, plan_required=False):
            """一个由 harness 驾驭的有界子阶段。返回结束工具的参数，或预算耗尽时 None。"""
            nonlocal total_input, total_output, global_turn, last_test_rc

            log("phase", {"phase": phase, "max_turns": max_turns})
            tool_names = {t["function"]["name"] for t in tools}
            prev_failed = False
            force_think = False
            fail_streak = 0
            plan_written = not plan_required   # plan_required 时，开局先写计划
            seen_commands: set[str] = set()

            for t in range(max_turns):
                global_turn += 1
                first = (t == 0)
                thinking_on = (first and think_first) or prev_failed or force_think
                force_think = False
                last_turn = (t == max_turns - 1)

                # 组装本轮请求：历史 + 阶段提示 + 预算提醒 + 工作记忆（都不进历史）。
                # 工作记忆钉在最底部（最靠近生成处），是抗遗忘的锚点。
                extras = [{"role": "system", "content": prompt}]
                if last_turn:
                    extras.append({"role": "system",
                                   "content": f"This is your LAST {phase} turn (budget {max_turns}). "
                                              f"Finish now: call {exit_tool}."})
                mem_text = render_memory()
                if mem_text:
                    extras.append({"role": "system", "content": mem_text})
                request_messages = messages + extras

                # 最后一轮、且本阶段无门禁：限制可用工具只剩 exit_tool，强制模型必须调它。
                # 注意：MiMo API 不响应 tool_choice 指定函数，只能靠限制 tools 列表来强制。
                force_exit = last_turn and gate is None
                active_tools = (
                    [t for t in tools if t["function"]["name"] == exit_tool]
                    if force_exit else tools
                )
                llm_kwargs = dict(
                    model=MODEL, messages=request_messages, tools=active_tools,
                    extra_body={"thinking": {"type": "enabled" if thinking_on else "disabled"}},
                )

                log("send", {"turn": global_turn, "phase": phase, "thinking": thinking_on,
                             "force_exit": force_exit, "messages": _to_serializable(request_messages)})
                resp = await call_llm(**llm_kwargs)
                msg = resp.choices[0].message
                if resp.usage:
                    total_input += resp.usage.prompt_tokens or 0
                    total_output += resp.usage.completion_tokens or 0
                await key_pool.record(key, resp.usage)  # 逐轮累计成本

                log("recv", {
                    "turn": global_turn, "phase": phase,
                    "thinking": getattr(msg, "reasoning_content", None),
                    "content": msg.content,
                    "tool_calls": _to_serializable(msg.tool_calls) if msg.tool_calls else None,
                    "usage": _to_serializable(resp.usage),
                })
                messages.append(msg)

                if not msg.tool_calls:
                    messages.append({"role": "user",
                                     "content": f"用工具来推进。本阶段完成后调用 {exit_tool}。"})
                    prev_failed = False
                    continue

                turn_failed = False
                exit_args = None
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    if name == exit_tool:
                        if gate is not None:
                            ok, reason = gate(args)
                            if not ok:
                                append_tool(tc.id, reason)
                                turn_failed = True
                                continue
                        exit_args = args
                        append_tool(tc.id, "(accepted)")
                        continue

                    if name == "update_plan":
                        if args.get("requirements"):
                            mem["requirements"] = args["requirements"]
                        if args.get("plan"):
                            mem["plan"] = args["plan"]
                        plan_written = True
                        log("memory", {"phase": phase, "memory": mem})
                        append_tool(tc.id, "(已更新工作记忆中的 requirements / plan)")
                        continue

                    if name in ("run_command", "run_test"):
                        command = args.get("command", "")
                        rc, content = await exec_shell(command, name, phase, global_turn)
                        if name == "run_test":
                            last_test_rc = rc
                        if rc != 0:
                            turn_failed = True
                        if command in seen_commands:
                            content += "\n[note] 你已运行过完全相同的命令；换个思路或先想清楚再试。"
                        seen_commands.add(command)
                        append_tool(tc.id, content)
                        continue

                    append_tool(tc.id, f"[error] unknown tool {name}")
                    turn_failed = True

                if exit_args is not None:
                    log("phase_exit", {"phase": phase, "turn": global_turn})
                    return exit_args

                # 开局要求先写计划：还没写就催一句（强制思考下一轮）
                if plan_required and not plan_written:
                    force_think = True
                    messages.append({"role": "user",
                                     "content": "动手前先调用 update_plan：基于上面的环境事实写清 requirements 和 plan，再开始实现。"})

                prev_failed = turn_failed
                fail_streak = fail_streak + 1 if turn_failed else 0
                if fail_streak >= revise_after:
                    force_think = True
                    fail_streak = 0
                    hint = "你已连续失败。停止试错，先想清楚失败的根本原因，再换一种方法。"
                    if "update_plan" in tool_names:
                        hint += "如果是计划本身走不通，调用 update_plan 重写计划（说明改了什么、为什么）。"
                    messages.append({"role": "user", "content": hint})
                elif turn_failed:
                    # 单轮失败：立即明确提醒模型有命令出错，需先修复再继续。
                    messages.append({"role": "user", "content": (
                        "上面有命令以非零退出码失败——先查清 [exit code: N] 后面的错误原因，"
                        "修复它，再继续下一步。不要跳过错误直接往下走。"
                    )})

            return None

        log("init", {"task": instruction, "model": MODEL, "max_turns": MAX_TURNS})

        # 3. 三个阶段，由 harness 顺序驱动；每个阶段结束更新工作记忆。
        # RECON：只勘察环境，结论并入工作记忆的「环境事实」（先探索，不写计划）。
        recon_args = await run_phase(
            "RECON", RECON_PROMPT, [RUN_COMMAND, REPORT_FINDINGS],
            "report_findings", RECON_MAX_TURNS, think_first=True)

        findings = (recon_args or {}).get("findings", "")
        if not findings:
            # MiMo API 不响应工具列表限制，模型可能跑完 budget 也不调 report_findings。
            # 补一次纯文本调用，让模型把对话历史里的勘察结果整理成结论。
            log("recon_summary_call", {"reason": "report_findings not called, extracting from history"})
            summary_resp = await call_llm(
                model=MODEL,
                messages=messages + [{"role": "user", "content": (
                    "根据你刚才运行的所有命令及其输出，用简短的结构化格式总结 RECON 结论："
                    "工作目录、关键文件路径、运行时版本、可用依赖（import 成功的）、"
                    "任务要求的函数/CLI 签名和精确输出格式、如何校验（测试文件/期望输出）、"
                    "以及如何运行解题文件（如 cd /app、PYTHONPATH 等）。"
                    "直接输出结论文本，不要运行任何命令。"
                )}],
                extra_body={"thinking": {"type": "enabled"}},
            )
            findings = summary_resp.choices[0].message.content or "(recon 无法总结)"
            if summary_resp.usage:
                total_input += summary_resp.usage.prompt_tokens or 0
                total_output += summary_resp.usage.completion_tokens or 0
            await key_pool.record(key, summary_resp.usage)

        mem["facts"] = findings
        mem["progress"].append("RECON 完成：已确认环境/路径/依赖")
        log("memory", {"phase": "RECON", "memory": mem})

        # IMPLEMENT：开局先 update_plan 据 RECON 写计划，再写解题代码；计划可改。
        impl_args = await run_phase(
            "IMPLEMENT", IMPLEMENT_PROMPT, [RUN_COMMAND, UPDATE_PLAN, FINISH_IMPLEMENTATION],
            "finish_implementation", IMPLEMENT_MAX_TURNS, think_first=True,
            plan_required=True)
        mem["progress"].append(
            "IMPLEMENT 完成：" + ((impl_args or {}).get("summary", "") or "已写好解题文件"))
        log("memory", {"phase": "IMPLEMENT", "memory": mem})

        # VERIFY：断言验收，严格门禁。
        verify_args = await run_phase(
            "VERIFY", VERIFY_PROMPT, [RUN_COMMAND, RUN_TEST, SUBMIT],
            "submit", VERIFY_MAX_TURNS, think_first=True, gate=verify_gate)
        mem["progress"].append(
            "VERIFY 完成：" + ((verify_args or {}).get("summary", "") or "已提交"))
        log("memory", {"phase": "VERIFY", "memory": mem})

        # 4. 收尾
        log("done", {
            "total_turns": global_turn,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
        })
        context.n_input_tokens = total_input
        context.n_output_tokens = total_output
        await key_pool.release(key)
