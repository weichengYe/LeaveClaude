from __future__ import annotations

from collections.abc import Callable

from leave_claude.core.context import ExecutionContext
from leave_claude.core.events.bus import EventBus
from leave_claude.core.llm.types import LlmResponse, ToolCallBlock
from leave_claude.core.loop import AgentLoop
from leave_claude.core.task.manager import TaskManager
from leave_claude.core.tools.base import BaseTool, ToolResult
from leave_claude.core.tools.registry import ToolRegistry


class _ScriptedProvider:
    # 按顺序返回预设响应；每一步调用前可执行钩子，模拟 Agent 的真实副作用（如更新任务状态）
    def __init__(
        self,
        responses: list[LlmResponse],
        hooks: list[Callable[[], None] | None] | None = None,
    ) -> None:
        self._responses = list(responses)
        self._hooks: list[Callable[[], None] | None] = list(hooks or [])
        self.calls = 0

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
        idx = self.calls
        self.calls += 1
        hook = self._hooks[idx] if idx < len(self._hooks) else None
        if hook is not None:
            hook()
        return self._responses[idx]


class _EchoTool(BaseTool):
    name = "echo"
    description = "Echoes msg"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=str(params["msg"]))


def _end_turn(text: str = "done") -> LlmResponse:
    return LlmResponse(stop_reason="end_turn", text=text)


def _echo_call(uid: str = "t1") -> LlmResponse:
    return LlmResponse(
        stop_reason="tool_use",
        tool_calls=[ToolCallBlock(id=uid, name="echo", input={"msg": "hi"})],
    )


def _ctx(max_steps: int = 8) -> ExecutionContext:
    return ExecutionContext(run_id="r1", goal="finish the plan", max_steps=max_steps)


def _loop(provider: _ScriptedProvider, manager: TaskManager | None, retries: int = 2) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    return AgentLoop(
        provider,  # type: ignore[arg-type]
        registry,
        EventBus(),
        task_manager=manager,
        max_completion_guard_retries=retries,
    )


def _reminders(context: ExecutionContext) -> list[str]:
    marker = "You attempted to finish the run, but planned tasks are still unfinished."
    return [
        m["content"]
        for m in context.messages
        if m["role"] == "user"
        and isinstance(m["content"], str)
        and m["content"].startswith(marker)
    ]


# 功能：验证未创建任何 Task 时，end_turn 仍然直接判定为 success（不破坏简单请求）
# 设计：TaskManager 为空 + 单步 end_turn，覆盖 Guard 的向后兼容路径，防止改造把简单任务误判为失败
async def test_no_tasks_end_turn_succeeds(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    loop = _loop(_ScriptedProvider([_end_turn()]), manager)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 1


# 功能：验证所有 Task 均 completed 时，end_turn 判定为 success
# 设计：构造两个任务并全部置为 completed，确认 Guard 只拦截未完成场景，不误伤正常收尾
async def test_all_completed_end_turn_succeeds(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("inspect")
    manager.create("implement")
    manager.update(1, status="completed")
    manager.update(2, status="completed")
    loop = _loop(_ScriptedProvider([_end_turn()]), manager)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.unfinished_tasks == []


# 功能：验证存在未完成任务时 end_turn 被拦截，注入结构化 reminder 并继续循环直到补做完成
# 设计：第一步提前 end_turn，用 hook 在第二步前把任务置为 completed，断言最终 success 且过程中产生了 reminder，覆盖"拦截→纠偏→恢复执行"主路径
async def test_unfinished_task_injects_reminder_and_continues(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("inspect")
    manager.create("Add tests")
    manager.update(1, status="completed")
    provider = _ScriptedProvider(
        [_end_turn("done early"), _end_turn("really done")],
        hooks=[None, lambda: manager.update(2, status="completed")],
    )
    loop = _loop(provider, manager)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 2
    assert provider.calls == 2
    assert len(_reminders(ctx)) == 1


# 功能：验证只要仍有未完成任务，end_turn 绝不被当作 success
# 设计：把重试上限设为 0，令第一次提前收尾立即失败，断言 status=failed/reason=unfinished_tasks，直接锁定本次改造的核心不变式
async def test_unfinished_task_never_successes_while_pending(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("inspect")
    manager.create("Add tests")
    manager.update(1, status="completed")
    loop = _loop(_ScriptedProvider([_end_turn("done early")]), manager, retries=0)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "unfinished_tasks"
    assert ctx.unfinished_tasks == [{"id": 2, "subject": "Add tests", "status": "pending"}]


# 功能：验证连续提前收尾耗尽重试上限后，以 unfinished_tasks 明确失败并保留诊断信息
# 设计：三次 end_turn + 默认重试上限 2，断言前两次注入 reminder、第三次失败，覆盖"有限纠偏不会死循环"
async def test_guard_retry_exhaustion_fails_with_diagnostics(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("Add tests")
    provider = _ScriptedProvider([_end_turn("done")] * 3)
    loop = _loop(provider, manager)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "unfinished_tasks"
    assert ctx.step == 3
    assert provider.calls == 3
    assert len(_reminders(ctx)) == 2
    assert ctx.unfinished_tasks == [{"id": 1, "subject": "Add tests", "status": "pending"}]


# 功能：验证一次真实工具调用会重置纠偏计数，只有"连续"提前收尾才累计
# 设计：重试上限 1，序列为 end_turn→tool_use→end_turn→完成→end_turn；若不重置计数第二次提前收尾就会失败，最终 success 反证重置生效
async def test_guard_retry_counter_resets_after_tool_use(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("Add tests")
    provider = _ScriptedProvider(
        [_end_turn("done early"), _echo_call(), _end_turn("still early"), _end_turn("done")],
        hooks=[None, None, None, lambda: manager.update(1, status="completed")],
    )
    loop = _loop(provider, manager, retries=1)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 4


# 功能：验证 max_steps 耗尽导致的失败同样携带未完成任务诊断
# 设计：任务保持 pending + 无限 tool_use 触发 max_steps，断言 reason=exceeded_max_steps 且 unfinished_tasks 非空，覆盖预算耗尽路径的可观测性
async def test_max_steps_failure_carries_unfinished_tasks(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("Add tests")
    provider = _ScriptedProvider([_echo_call(f"t{i}") for i in range(10)])
    loop = _loop(provider, manager)
    ctx = _ctx(max_steps=3)
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "exceeded_max_steps"
    assert ctx.unfinished_tasks == [{"id": 1, "subject": "Add tests", "status": "pending"}]


# 功能：验证注入的 reminder 是结构化的：包含未完成任务列表与继续执行的明确指引
# 设计：检查 reminder 文本同时包含任务编号、标题、状态和 task_list 提示，确保模型拿到的是可操作信息而非一句空话
async def test_reminder_is_structured(tmp_path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("Add tests")
    provider = _ScriptedProvider([_end_turn("done"), _end_turn("done")])
    loop = _loop(provider, manager)
    ctx = _ctx()
    await loop.run(ctx)
    reminder = _reminders(ctx)[0]
    assert "#1 Add tests [pending]" in reminder
    assert "Before finishing:" in reminder
    assert "task_list" in reminder


# 功能：验证未注入 TaskManager 时 Guard 完全旁路，保持原有成功语义
# 设计：与 test_end_turn_marks_success 等价但显式传入 task_manager=None，锁定"可选依赖"的兼容契约
async def test_absent_task_manager_keeps_legacy_behavior(tmp_path) -> None:
    loop = _loop(_ScriptedProvider([_end_turn("done")]), None)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.unfinished_tasks == []
