# main.py
import json
import os

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from openai import OpenAI

MODEL = "deepseek-v4-pro"
MAX_TURNS = 30
MAX_OUTPUT_CHARS = 100000

SYSTEM = """You are a terminal agent operating in a Linux container.
Solve the task by executing bash commands, one at a time.
Observe each result before deciding the next command.
When the task is complete, reply with plain text instead of calling a tool."""

# OpenAI tool format (note the extra "function" nesting vs Anthropic's flat format)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a bash command in the container and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


class DeepSeekAgent(BaseAgent):
    @staticmethod
    def name() -> str:
        return "seqi-deepseek-agent"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/beta",
        )
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Task:\n{instruction}"},
        ]

        for turn in range(MAX_TURNS):
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                reasoning_effort="high",
                extra_body={"thinking": {"type": "enabled"}},
                # Optional deeper reasoning (thinking mode):
                # extra_body={"thinking": {"type": "enabled"}},
            )
            msg = response.choices[0].message

            # CRITICAL: append the full message object, not a copy.
            # In thinking mode it carries reasoning_content, which the API
            # requires back on tool-call turns.
            messages.append(msg)

            if not msg.tool_calls:
                break  # plain-text reply = model says it's done

            for tool_call in msg.tool_calls:
                args = json.loads(tool_call.function.arguments)
                result = await environment.exec(args["command"])
                output = str(result)[:MAX_OUTPUT_CHARS]

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": output,
                    }
                )

            context.metadata = {"turns": turn + 1}
