"""API key pool with per-key cost tracking and failure-based rotation."""

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

# ── 官方定价（USD/百万 token，来源：xiaomimimo.com）────────────────────────
PRICING_USD_PER_M: dict[str, dict[str, float]] = {
    "mimo-v2.5-pro": {
        "input_cached": 0.0036,
        "input":        0.435,
        "output":       0.87,
    },
    "mimo-v2.5": {
        "input_cached": 0.0028,
        "input":        0.14,
        "output":       0.28,
    },
}

BUDGET_LIMIT_USD = 2.7  # 仅用于展示预警，不自动禁用 key


@dataclass
class KeyStats:
    key_hint: str
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tasks_completed: int = 0
    active_tasks: int = 0
    failed: bool = False      # 仅当 API 实际报错时才置为 True
    fail_reason: str = ""     # 失败原因


class NoAvailableKeyError(RuntimeError):
    """所有 key 均已失效（API 报错），无可用 key。"""


class KeyPool:
    """
    asyncio-safe API key pool.

    key 失效条件：API 实际返回认证/余额错误，而非预估花费超限。
    预算追踪仅用于展示，不影响 key 的可用状态。
    """

    def __init__(self, keys: list[str], model: str, state_path: Path) -> None:
        self._keys = keys
        self._model = model
        self._state_path = state_path
        self._lock = asyncio.Lock()
        self._stats: dict[str, KeyStats] = {}
        self._load()

    @classmethod
    def from_env(cls, model: str, state_path: Path | None = None) -> "KeyPool":
        raw = os.environ.get("MIMO_API_KEYS", "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not keys:
            single = os.environ.get("MIMO_API_KEY", "")
            if single:
                keys = [single]
        if not keys:
            raise RuntimeError("No MIMO_API_KEYS or MIMO_API_KEY found in environment")
        if state_path is None:
            state_path = Path("jobs/key_pool_state.json")
        return cls(keys, model, state_path)

    # ── 持久化 ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        saved: dict[str, dict] = {}
        if self._state_path.exists():
            try:
                saved = json.loads(self._state_path.read_text())
            except Exception:
                pass

        for key in self._keys:
            d = saved.get(key, {})
            self._stats[key] = KeyStats(
                key_hint=key[:20],
                prompt_tokens=d.get("prompt_tokens", 0),
                cached_tokens=d.get("cached_tokens", 0),
                completion_tokens=d.get("completion_tokens", 0),
                reasoning_tokens=d.get("reasoning_tokens", 0),
                cost_usd=d.get("cost_usd", 0.0),
                tasks_completed=d.get("tasks_completed", 0),
                active_tasks=0,
                failed=d.get("failed", False),
                fail_reason=d.get("fail_reason", ""),
            )

    def _save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {k: asdict(s) for k, s in self._stats.items()}, indent=2
        ))
        tmp.replace(self._state_path)

    # ── 公开接口 ─────────────────────────────────────────────────────────────

    async def acquire(self) -> str:
        """返回当前可用的 key（按插入顺序，用完一个再换下一个）。"""
        async with self._lock:
            for key in self._keys:
                if not self._stats[key].failed:
                    self._stats[key].active_tasks += 1
                    return key
            raise NoAvailableKeyError(
                f"All {len(self._stats)} API keys have failed. "
                "Check fail_reason in jobs/key_pool_state.json."
            )

    async def record(self, key: str, usage) -> None:
        """记录 token 消耗，仅做统计，不影响 key 可用状态。"""
        pricing = PRICING_USD_PER_M.get(self._model)
        if not pricing or usage is None:
            return

        prompt     = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        comp_det   = getattr(usage, "completion_tokens_details", None)
        reasoning  = getattr(comp_det, "reasoning_tokens", 0) or 0
        prom_det   = getattr(usage, "prompt_tokens_details", None)
        cached     = getattr(prom_det, "cached_tokens", 0) or 0

        cost = (
            (prompt - cached) * pricing["input"]        / 1_000_000 +
            cached            * pricing["input_cached"] / 1_000_000 +
            completion        * pricing["output"]       / 1_000_000
        )

        async with self._lock:
            s = self._stats[key]
            s.prompt_tokens     += prompt
            s.cached_tokens     += cached
            s.completion_tokens += completion
            s.reasoning_tokens  += reasoning
            s.cost_usd          += cost
            self._save()

    async def mark_failed(self, key: str, reason: str = "") -> None:
        """API 实际报错时调用，永久禁用该 key。"""
        async with self._lock:
            s = self._stats[key]
            s.failed = True
            s.fail_reason = reason
            s.active_tasks = max(0, s.active_tasks - 1)
            self._save()

    async def release(self, key: str) -> None:
        """任务正常结束。"""
        async with self._lock:
            s = self._stats[key]
            s.active_tasks = max(0, s.active_tasks - 1)
            s.tasks_completed += 1
            self._save()

    def summary(self) -> str:
        header = (
            f"\n{'Key':<22} {'Tasks':>6} {'Input':>10} {'Cached':>8} "
            f"{'Output':>10} {'Reasoning':>10} {'Cost USD':>10} {'Budget':>8} {'Status'}"
        )
        sep = "─" * 96
        lines = [header, sep]

        total_p = total_ca = total_c = total_r = 0
        total_cost = 0.0

        for s in self._stats.values():
            if s.failed:
                status = f"FAILED: {s.fail_reason[:20]}" if s.fail_reason else "FAILED"
            elif s.active_tasks > 0:
                status = f"ACTIVE({s.active_tasks})"
            elif s.cost_usd >= BUDGET_LIMIT_USD:
                status = f"WARN>${BUDGET_LIMIT_USD}"
            else:
                status = "OK"

            budget_pct = f"{s.cost_usd / BUDGET_LIMIT_USD * 100:.0f}%"
            lines.append(
                f"{s.key_hint:<22} {s.tasks_completed:>6} "
                f"{s.prompt_tokens:>10,} {s.cached_tokens:>8,} "
                f"{s.completion_tokens:>10,} {s.reasoning_tokens:>10,} "
                f"${s.cost_usd:>9.4f} {budget_pct:>8} {status}"
            )
            total_p    += s.prompt_tokens
            total_ca   += s.cached_tokens
            total_c    += s.completion_tokens
            total_r    += s.reasoning_tokens
            total_cost += s.cost_usd

        lines += [
            sep,
            f"{'TOTAL':<22} "
            f"{sum(s.tasks_completed for s in self._stats.values()):>6} "
            f"{total_p:>10,} {total_ca:>8,} {total_c:>10,} {total_r:>10,} "
            f"${total_cost:>9.4f}",
        ]
        return "\n".join(lines)
