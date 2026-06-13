"""
运行这个脚本，观察 Agent 循环中每一轮的完整数据结构。
用法：uv run python inspect_messages.py
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

from mimo_agent.config import MODEL

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "在 bash 终端里执行一条命令，返回输出结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    }
]

KEYS = os.environ.get("MIMO_API_KEYS", "").split(",")
client = AsyncOpenAI(api_key=KEYS[0].strip(), base_url="https://api.xiaomimimo.com/v1")


def to_serializable(obj):
    """把 OpenAI 对象递归转成普通 dict/list，方便 json.dumps。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [to_serializable(i) for i in obj]
    if isinstance(obj, dict):
        return {k: to_serializable(v) for k, v in obj.items()}
    return obj


def show(label: str, data):
    """格式化打印，便于阅读。"""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(json.dumps(to_serializable(data), indent=2, ensure_ascii=False))


async def main():
    # ── 初始消息列表 ─────────────────────────────────────────────────────────
    messages = [
        {"role": "system", "content": "你是一个终端智能体，用命令完成任务。"},
        {"role": "user",   "content": "用 echo 命令打印出 hello world"},
    ]

    # ════════════════════════════════════════════════════════════════
    # 第 1 轮
    # ════════════════════════════════════════════════════════════════
    print("\n\n★ 第 1 轮 ★")

    # 【发送】完整的 messages 列表
    show("【发送给 LLM 的 messages】", messages)

    response = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        extra_body={"thinking": {"type": "disabled"}},  # 关闭思考，输出更简洁
    )

    # 【接收】LLM 的完整回复
    show("【LLM 返回的 response.choices[0].message】", response.choices[0].message)
    show("【token 用量 response.usage】", response.usage)

    msg = response.choices[0].message

    # 把 LLM 的回复加入 messages
    messages.append(msg)

    # ════════════════════════════════════════════════════════════════
    # 执行工具调用（模拟 environment.exec）
    # ════════════════════════════════════════════════════════════════
    if msg.tool_calls:
        for tool_call in msg.tool_calls:
            args = json.loads(tool_call.function.arguments)
            command = args["command"]

            print(f"\n>>> 执行命令: {command}")
            fake_output = "hello world\n"  # 模拟容器执行结果

            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": fake_output,
            }
            show("【追加进 messages 的 tool 消息】", tool_message)
            messages.append(tool_message)

    # ════════════════════════════════════════════════════════════════
    # 第 2 轮：把工具结果喂给 LLM，让它总结
    # ════════════════════════════════════════════════════════════════
    print("\n\n★ 第 2 轮 ★")
    show("【发送给 LLM 的完整 messages（包含历史）】", messages)

    response2 = await client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        extra_body={"thinking": {"type": "disabled"}},
    )

    show("【LLM 返回的 response.choices[0].message】", response2.choices[0].message)

    msg2 = response2.choices[0].message
    if not msg2.tool_calls:
        print("\n✅ LLM 没有再调用工具 → 任务完成")
        print(f"\nLLM 的最终回答：{msg2.content}")


asyncio.run(main())
