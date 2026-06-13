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
    }
]

SYSTEM_PROMPT = """\
You are a terminal agent inside a Docker container. Complete the given task by running shell commands.
Think step by step before each command. When the task is fully done, stop calling tools and explain what you did.
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

        # 日志文件：每一轮追加一行 JSON，完整记录对话过程
        trace_path = Path(self.logs_dir) / "api_trace.jsonl"

        def log(event: str, data: dict):
            with trace_path.open("a") as f:
                f.write(json.dumps({"event": event, **data}, ensure_ascii=False) + "\n")

        # 记录任务开始
        log("init", {"task": instruction, "model": MODEL, "max_turns": MAX_TURNS})

        # 3. Agent 循环：最多跑 MAX_TURNS 轮
        for turn in range(MAX_TURNS):
            self.logger.info(f"[turn {turn + 1}] 发送消息给 LLM ...")

            # ── Think：让 LLM 决定下一步做什么 ─────────────────────────────
            # 记录本轮发送给 LLM 的完整 messages（含所有历史）
            log("send", {"turn": turn + 1, "messages": _to_serializable(messages)})

            last_response = await client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                extra_body={"thinking": {"type": "enabled"}},
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
            for tool_call in msg.tool_calls:
                args = json.loads(tool_call.function.arguments)
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

                self.logger.info(f"[turn {turn + 1}] exit={result.return_code}  output={combined[:200]}")

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
