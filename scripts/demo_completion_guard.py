#!/usr/bin/env python3
"""Task Completion Guard 的 Before / After 演示（离线、可复现，不调用真实 LLM）。

运行：uv run python scripts/demo_completion_guard.py
"""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from pathlib import Path

from leave_claude.core.context import ExecutionContext
from leave_claude.core.events.bus import EventBus
from leave_claude.core.llm.types import LlmResponse
from leave_claude.core.loop import AgentLoop
from leave_claude.core.task.manager import TaskManager
from leave_claude.core.tools.registry import ToolRegistry


class _ScriptedProvider:
    # 按顺序返回预设响应；每次调用前执行钩子，用来模拟 Agent 更新任务状态
    def __init__(
        self,
        responses: list[LlmResponse],
        hooks: list[Callable[[], None] | None] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._hooks: list[Callable[[], None] | None] = list(hooks or [])

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        response = self._responses.pop(0)
        if self._hooks:
            hook = self._hooks.pop(0)
            if hook is not None:
                hook()
        return response


# 构造"提前收尾"的预设响应
def _done_early() -> LlmResponse:
    return LlmResponse(stop_reason="end_turn", text="Done.")


# 构造与文档一致的初始计划：#1/#2 已完成，#3/#4 仍未完成
def _make_tasks(directory: str) -> TaskManager:
    manager = TaskManager(Path(directory))
    manager.create("Inspect CLI and current run command")
    manager.create("Implement --file support")
    manager.create("Add tests")
    manager.create("Run full verification")
    manager.update(1, status="completed")
    manager.update(2, status="completed")
    return manager


# 一次性把剩余任务全部标记完成，模拟 Agent 收到 reminder 后补做
def _complete_remaining(manager: TaskManager) -> Callable[[], None]:
    def _hook() -> None:
        for task in manager.unfinished_tasks():
            manager.update(task.id, status="completed")

    return _hook


# 打印一次 run 结果与残留任务，作为 Before / After 的对照输出
def _report(title: str, context: ExecutionContext, manager: TaskManager) -> None:
    print(f"\n=== {title} ===")
    print(f'  LLM 最后一句 : "{context.result}"')
    print(f"  RunOutcome   : status={context.status} reason={context.reason}")
    print(f"  剩余未完成任务: {manager.format_list().replace(chr(10), ' | ')}")
    if context.unfinished_tasks:
        print(f"  诊断详情     : {context.unfinished_tasks}")


# 运行一次 AgentLoop 并返回 context，供各场景复用
async def _run(
    provider: _ScriptedProvider, manager: TaskManager | None, *, retries: int = 2
) -> ExecutionContext:
    context = ExecutionContext(run_id="demo", goal="add leave run --file", max_steps=12)
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        task_manager=manager,
        max_completion_guard_retries=retries,
    )
    await loop.run(context)
    return context


# 主流程：依次演示改造前误判、改造后安全失败、改造后纠偏成功三条路径
async def main() -> None:
    with tempfile.TemporaryDirectory() as before_dir:
        manager = _make_tasks(before_dir)
        provider = _ScriptedProvider([_done_early() for _ in range(3)])
        context = await _run(provider, None)
        _report("Before：无 Guard，LLM 提前 end_turn", context, manager)
        print("  → 结论：仍有 pending 任务，却被当成 success（问题场景）")

    with tempfile.TemporaryDirectory() as after_dir:
        manager = _make_tasks(after_dir)
        provider = _ScriptedProvider([_done_early() for _ in range(3)])
        context = await _run(provider, manager)
        _report("After：有 Guard，连续提前 end_turn", context, manager)
        print("  → 结论：不再误判成功，失败结果携带 unfinished_tasks")

    with tempfile.TemporaryDirectory() as recover_dir:
        manager = _make_tasks(recover_dir)
        provider = _ScriptedProvider(
            [_done_early(), _done_early()],
            hooks=[None, _complete_remaining(manager)],
        )
        context = await _run(provider, manager)
        _report("After：有 Guard，收到 reminder 后补做完成", context, manager)
        print("  → 结论：纠偏生效，全部 completed 后才 success")


if __name__ == "__main__":
    asyncio.run(main())
