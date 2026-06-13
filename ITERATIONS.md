# 迭代记录：Agent 工作方法的问题与改进

每一轮观察 trace（`api_trace.jsonl` + `trace_viewer.py`）→ 找出问题 → 改进 → 重跑验证。
基准任务：`terminal-bench/cancel-async-tasks`（实现带并发上限的 `async run_tasks`）。

---

## 第 1 轮 —— 工作方法重塑（探索 / 思考 / 并行 / 验证）

**观察（reward=0，跑满 20 轮，227,712 input tokens）**
1. 一次干太多：没充分探索就一次性写完整个 `run.py`，随后反复撞 import / `PYTHONPATH` 错。
2. 思考离行动太远：为了跑 `pwd && ls` 思考 ~8K 字；还跑偏去测任务没要求的 SIGINT 行为。
3. 一次只发一条命令：20 轮每轮 1 个 tool_call，recon 被拆得很碎。
4. 假"通过"：自称完成但 reward=0，从没构造真正校验并发上限的测试。

**改进（`mimo_agent/agent.py`）**
1. `SYSTEM_PROMPT` 改成三阶段工作流 `<explore environment>` → `<implement>` → `<verify>`，
   硬规则"recon 确认依赖可 import、签名/路径正确前不许写解题代码"。
2. 提示词"只规划接下来 1–3 步、据实重规划、严格围绕任务真实要求"；代码加按阶段动态思考开关
   `thinking_on = (turn==0) or 上一轮有命令失败`，首轮思考保留并复用。
3. `create()` 加 `parallel_tool_calls=True`，提示词鼓励 recon 时一次发多个独立命令。
4. PHASE C 强制：写带 `assert` 的测试，错误结果必须非零退出。

**结果（reward=1，9 轮，28,024 tokens）**
- 探索→实现→验证顺序正确；token 降到 1/8；失败轮自动重开思考；用断言测试自检通过。
- 遗留：模型仍偏好 `&&` 拼接而非多 tool_call（功能上已批量，形式未走并行工具调用）。

---

## 第 2 轮 —— 假"ok"：模型看不到退出码

**观察**
模型常写 `print('ok')` 式代码，即使执行出错最后也打印 ok，导致它误判成功。
根因：回传给 LLM 的 tool 结果只有 `stdout+stderr`，**退出码根本没给模型看**，
它只能靠脚本打印的字判断。

**改进（`mimo_agent/agent.py`）**
- 代码：每条 tool 结果开头加 `[exit code: N]`（放最前、不被截断）。
- 提示词：PHASE C 与全局铁律"按 `[exit code]` 判断，不按打印的词；检验用会大声失败的
  `assert`/`set -e`/raise，绝不无条件 `print('ok')`"。

**结果**
模型能看到 `[exit code: 1]` 后持续修复、不再假通过（见第 1 轮的 reward=1 已包含此修复）。

---

## 第 3 轮 —— 工作记忆：关键信息丢失 / 重复推导

**观察（reward=1 但过程不理想）**
模型在 turn4 又踩 `No module named 'run'`，turn5/6 才靠 `PYTHONPATH=/app` 救回——
它**重新推导**了早就知道的"run.py 在 /app、导入要设路径"。根因：没有结构化、
始终在对话最下面的记忆位，长对话里碎片信息丢失。

**改进（`mimo_agent/agent.py`）**
- 新增 `update_memory(notes)` 工具：覆盖式更新工作记忆（环境信息、文件路径与如何
  运行/导入、函数签名、已验证内容）。
- 每轮把当前记忆作为一条 `## 工作记忆` 消息注入到对话**最底部**（不进历史，每轮重建，
  始终唯一且最新）。
- 提示词要求**每个阶段结束、即将进入下一阶段时调用一次** `update_memory`（不每轮、
  不中途），行动前先看记忆。

**结果（reward=1，8 轮，18,471 tokens）**
- 不再出现 `ModuleNotFoundError`：从 t5 起就用 `cd /app`，路径信息被记忆带住。
- 记忆按阶段更新（recon 结束 t3、verify 结束 t7 各一次）。
- 附带收获：**并行工具调用真的生效了**——t1、t2 各发了 2 个 `run_command`，
  recon 真正批量化（不再靠 `&&` 拼）。token 进一步降到 18K。

**收紧"每阶段只更新一次记忆"后复跑（reward=1，12 轮，36,528 tokens）**
- 记忆严格按阶段更新：t3（recon 结束）、t11（verify 结束）各一次，无每轮刷屏。
- 完整闭环演示：t8 的断言测试逮到 `run.py` 真 bug → `[exit code: 1]` → t9 因上轮失败
  重开思考（2596 字）修代码 → t10 重验 rc=0 → 通过。验证手段真的拦下了错误，
  而不是假"通过"。
