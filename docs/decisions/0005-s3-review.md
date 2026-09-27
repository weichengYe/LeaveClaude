# ADR 0005：S3 Task Planning 系统强化——保留任务 DAG、收紧状态约束并让 AgentLoop 感知任务状态

- **Status**: Proposed
- **Date**: 2026-09-26
- **Scope**: LeaveClaude / KamaClaude S3 Task Planning，兼容后续 S4–S7 架构
- **Decision owners**: LeaveClaude
- **Related stages**: S3, S4, S5, S6, S7

---

## 1. 背景

S3 引入了基于工具调用的任务规划机制：

```text
LLM / AgentLoop
    │
    ├── task_create
    ├── task_update
    ├── task_list
    └── task_get
            │
            ▼
       TaskManager
            │
       .tasks/*.json
```

该设计保持了 AgentLoop 的 ReAct 结构，不引入独立 Planner / Scheduler，而是让 LLM 自主决定何时创建任务、设置依赖、推进状态。

真实运行表明，这套机制能够完成较复杂的 Coding Agent 工作。例如一次 `leave run --file` 功能实现中，Agent 建立了：

```text
#1 Design & review
        ↓
#2 Implement argparse --file
        ↓
#3 Add tests
        ↓
#4 Run verification
```

并按照 `pending → in_progress → completed` 推进，最终成功完成任务。

但是多轮运行也暴露出：当前 Task 系统更接近 **structured planning memory / Todo state**，尚未成为 Agent Harness 的真正执行控制平面。

---

## 2. S4–S7 复查结论

复查 KamaClaude `stage/s4`、`stage/s5`、`stage/s6`、`stage/s7` 后发现：

### 2.1 TaskManager 在 S4–S7 没有继续演进

以下四个分支中的：

```text
src/kama_claude/core/task/manager.py
```

使用相同 Git blob SHA：

```text
8c27a5548dbb0d095b0c19f69f0753cf76899a63
```

因此 S3 TaskManager 的核心语义一直保留到 S7，包括：

- `blocked_by` 既表示任务依赖，又表示“当前未解除依赖”；
- completed 后调用 `_clear_dependency()` 删除其他任务中的依赖 ID；
- `update()` 可任意修改 `pending / in_progress / completed`；
- 不检查 blocked task 是否可以进入 `in_progress`；
- 不检测 self dependency / cycle；
- `_save()` 直接覆盖 JSON 文件；
- `list_all()` 对部分损坏任务直接跳过。

### 2.2 S7 AgentLoop 仍然不感知 TaskManager

S7 AgentLoop 新增了：

- permission manager；
- context compaction；
- session id；
- thinking block；
- max_tokens 异常补偿；

但终止逻辑仍是：

```text
stop_reason == end_turn
    → success

step >= max_steps
    → failed(exceeded_max_steps)
```

AgentLoop 没有 TaskManager 引用，也不会在成功退出前检查 unfinished tasks。

### 2.3 S7 增加 Session，但不是 Task checkpoint/resume

S7 SessionManager 支持：

- session create；
- thread 持久化；
- session resume；
- multi-turn conversation；
- compaction；
- note persistence。

但是每个新的 run 仍然创建：

```text
<run_path>/.tasks/
```

即 TaskManager 仍然是 **per-run**。

因此：

```text
session continuation ≠ task graph continuation
```

若某一 run 因 `max_steps` 失败，下一次 session message 可以继续上下文，但不会自动恢复上一 run 的 Task DAG、当前 task 或 dependency state。

### 2.4 S7 增加 Subagent 并行，但没有接入 Task DAG

S7 已支持：

```text
spawn_agent(run_in_background=true)
agent_result(run_id)
```

因此“没有并行 Agent 能力”这一问题在 S7 已得到解决。

但是：

- parent TaskManager 不会调度 subagent；
- Task 节点没有 `assigned_run_id / subagent_id`；
- subagent 自己创建独立 `.tasks`；
- Task DAG 与 BackgroundTaskRegistry 是两个独立系统。

因此只能认为：

```text
并行 Subagent 能力：已解决
Task DAG 自动并行调度：未解决
```

### 2.5 Task UI 仍不是一等 UI

S7 TUI 仍主要通过：

```text
tool.call_started
tool.call_finished
```

把 `task_create / task_update / task_list` 当作普通 ToolCallBlock 展示。

事件模型中没有：

```text
task.created
task.updated
task.completed
```

因此仍缺少稳定的 Task Board / DAG 展示接口。

---

## 3. S3 问题到 S7 的状态矩阵

| 问题 | S7 状态 | 说明 |
|---|---|---|
| `blocked_by` 完成后被删除，历史 DAG 丢失 | ❌ 未解决 | TaskManager S4–S7 未变化 |
| blocked task 仍可直接 `in_progress` | ❌ 未解决 | update 无依赖检查 |
| 状态可任意回退/跳跃 | ❌ 未解决 | 无 transition state machine |
| 不检测 self dependency / cycle | ❌ 未解决 | 仅 create 时检查依赖文件存在 |
| AgentLoop 不检查 unfinished tasks | ❌ 未解决 | end_turn 仍直接 success |
| 复杂任务 planning 可能出现过晚 | ❌ 未解决 | system prompt 仍仅要求完成 goal |
| max_steps 是硬悬崖 | ❌ 未解决 | 无预算提醒 / graceful checkpoint |
| max_steps 后自动恢复 Task | ❌ 未解决 | Session 持久化不包含跨 run Task DAG |
| Task description 无法重规划更新 | ❌ 未解决 | task_update 仅 status / blocked_by |
| `completed` 无验证证据 | ❌ 未解决 | Task Model 无 acceptance/evidence |
| Task scope 膨胀后无 child task 结构 | ❌ 未解决 | Task 无 parent_id |
| Task 专用事件 / TUI Task Board | ❌ 未解决 | 仍通过通用 tool events |
| Task JSON 原子写入 | ❌ 未解决 | 直接 `Path.write_text()` |
| 并行 Subagent | ✅ 已解决 | S7 SpawnAgentTool 支持后台并行 |
| Task DAG 自动并行调度 | ❌ 未解决 | Subagent registry 与 TaskManager 独立 |
| 多轮会话上下文恢复 | ✅ 已解决 | SessionManager / SessionStore |
| Task DAG 跨 run 恢复 | ❌ 未解决 | TaskManager 仍位于 run_path/.tasks |
| Context 长度压力 | ✅/部分 | S7 compaction 可缓解，但与 Task budget 无关 |

---

## 4. 决策

### 4.1 保留 S3 的总体架构

不引入完整 Planner–Executor Workflow Engine。

继续采用：

```text
ReAct AgentLoop
    +
Task Tools
    +
TaskManager
```

原因：

1. 已通过真实 Coding Agent 任务验证可工作；
2. 结构简单，适合当前 LeaveClaude 阶段；
3. 后续 S4–S7 的 Session / Permission / Subagent / MCP 等能力均建立在现有 AgentLoop 周围；
4. 当前主要问题是 Task 状态语义和 Harness 约束不足，而非缺少完整 Scheduler。

---

## 4.2 将“依赖结构”和“运行时阻塞状态”分离

废弃：

```python
blocked_by: list[int]
```

作为唯一依赖表示。

改为：

```python
depends_on: list[int]
```

永久保存原始 DAG。

运行时动态计算：

```python
def unresolved_dependencies(task_id: int) -> list[int]:
    ...
```

例如：

```text
Task #2 depends_on=[1]

#1 pending
→ unresolved=[1]

#1 completed
→ unresolved=[]
```

完成 #1 时不再删除 #2 的依赖边。

### 结果

即使所有任务完成后，仍能恢复：

```text
#1 → #2 → #3 → #4
```

用于：

- 调试；
- TUI DAG 展示；
- Run 复盘；
- 后续调度；
- 统计规划质量。

---

## 4.3 TaskManager 增加状态机和依赖校验

最小合法状态流：

```text
pending
   │
   │ unresolved_dependencies == []
   ▼
in_progress
   │
   ▼
completed
```

第一阶段不增加复杂 failure state，但禁止：

```text
pending → completed
completed → pending
completed → in_progress
blocked pending → in_progress
```

同时校验：

- dependency task 必须存在；
- task 不能依赖自身；
- 新增 dependency 后不得形成环；
- 依赖列表保持稳定顺序，不使用 `set()` 破坏顺序。

推荐 API：

```python
TaskManager.start(task_id)
TaskManager.complete(task_id)
TaskManager.update_plan(...)
```

而不是让 Tool 直接任意写 status。

---

## 4.4 AgentLoop 增加 Task Completion Guard

AgentLoop 可以接收可选的 TaskManager：

```python
AgentLoop(
    provider,
    registry,
    bus,
    task_manager=task_manager,
)
```

当：

```text
response.stop_reason == "end_turn"
```

时：

```text
没有创建过 Task
    → 正常 success

创建过 Task 且全部 completed
    → success

创建过 Task 且仍有 unfinished
    → 不 success
    → 向 context 注入 reminder
    → 继续下一轮
```

建议提示：

```text
You still have unfinished tasks.
Review task_list and either complete them,
re-plan them, or explicitly explain why they should be removed.
```

该机制不要求简单请求强制建 Task。

---

## 4.5 增加轻量 Planning Policy，而不是独立 Planner

不要求每个请求都先创建 Task。

对于明显多阶段 Coding Task：

```text
inspect
→ modify
→ test
→ verify
```

System Prompt 增加规则：

```text
For multi-step coding tasks:
1. inspect only enough context to understand the task;
2. create a task plan before substantial edits;
3. encode ordering with dependencies;
4. keep task status up to date;
5. re-plan when new information invalidates the original plan.
```

可选 Harness reminder：

```text
如果已执行 N 个 exploration step
且尚未产生 write/tool side effect
且尚未创建 Task
→ 注入 planning reminder
```

第一阶段不做复杂任务分类器。

---

## 4.6 Step Budget 从“硬上限”升级为“可感知预算”

保留 `max_steps` 作为最终安全阈值，但增加阶段提醒：

```text
70%：提醒剩余预算
85%：要求停止可选探索，优先 unfinished tasks
95%：禁止启动大范围新探索，优先验证/收尾
100%：failed(exceeded_max_steps)
```

例如：

```text
Step budget: 85/100 used.
Prioritize unfinished tasks and validation.
Avoid optional exploration.
```

本 ADR 暂不实现真正自动续跑。

---

## 4.7 Session continuation 与 Task continuation 暂时分离

S7 已经支持 multi-turn Session。

本 ADR 不将 TaskManager 改成 session-global，因为：

- 不同 run 可能代表不同用户请求；
- session-global DAG 会引入旧任务污染问题；
- 需要额外定义 task ownership / lifecycle。

但是在 `exceeded_max_steps` 时，应在 RunOutcome / Session thread 中保存：

```text
unfinished task summary
current task
verification state
```

作为下一 run 的恢复提示。

真正的 Task graph resume 作为后续 ADR 处理。

---

## 4.8 增加 Replanning 能力

`task_update` 增加：

```text
subject
description
depends_on
```

允许执行中因新证据调整计划。

例如：

```text
初始：
#2 Extract build_parser()

发现 private argparse API 不合理后：

#2 Minimal CLI wiring without parser extraction
```

所有变更仍通过 events/trace 留痕。

---

## 4.9 completed 增加 completion evidence

Task Model 增加可选字段：

```python
acceptance_criteria: list[str]
completion_note: str
verification: list[str]
```

示例：

```json
{
  "acceptance_criteria": [
    "--goal and --file are mutually exclusive",
    "missing file exits 1",
    "unit tests pass"
  ],
  "completion_note": "CLI wiring implemented and targeted tests passed.",
  "verification": [
    "uv run pytest tests/unit/test_run_goal_file.py -q",
    "uv run mypy src"
  ]
}
```

第一阶段不引入独立 Evaluator Agent。

---

## 4.10 Task 持久化改为原子写

当前：

```python
path.write_text(...)
```

改为：

```text
task_1.json.tmp
    ↓ write / flush
os.replace()
    ↓
task_1.json
```

同时：

```text
JSON parse error
```

不得在 `list_all()` 中静默忽略。

至少：

- log error；
- 保留错误文件；
- 返回可诊断信息。

---

## 4.11 Task 成为一等事件对象

增加：

```text
task.created
task.updated
task.started
task.completed
task.replanned
```

事件。

TUI 不再只能从 `tool.call_*` 推断 Task 状态。

后续可以展示：

```text
Tasks
────────────────────────────────
✓ #1 Design
✓ #2 Implement
▶ #3 Tests
○ #4 Verify      depends on #3
```

Task Board 属于 P1，可在核心 TaskManager hardening 后实现。

---

## 5. 暂不解决的问题

本 ADR 明确不实现：

### 5.1 完整 Task Scheduler

暂不做：

```text
自动 next runnable
DAG 拓扑调度
任务优先级
deadline
自动并行执行
```

LLM 仍负责选择当前 task。

### 5.2 Task 与 Subagent 自动绑定

S7 已有：

```text
spawn_agent
agent_result
```

但本 ADR 不设计：

```text
Task #5
assigned_to=subagent-X
```

该能力应在真实出现“一个主 Agent 需要调度多个并行 Worker”的需求后单独设计。

### 5.3 Parent / Child Task Tree

先保留扁平 DAG。

如果后续频繁出现：

```text
Verify
├── investigate failure
├── smoke test
└── update docs
```

再引入：

```python
parent_id
```

---

## 6. 推荐实施顺序

### Phase A — Task 数据模型修正（P0）

修改：

```text
core/task/model.py
core/task/manager.py
core/tools/builtin/task_create.py
core/tools/builtin/task_update.py
tests/unit/test_task_model.py
tests/unit/test_task_manager.py
```

完成：

1. `blocked_by → depends_on`
2. unresolved 动态计算
3. transition guard
4. dependency validation
5. cycle detection
6. atomic write

### Phase B — AgentLoop Task-aware（P0）

修改：

```text
core/runner.py
core/loop.py
core/context.py（若需要 reminder）
tests/unit/test_loop.py
tests/unit/test_runner.py
```

完成：

1. AgentLoop 注入 TaskManager
2. end_turn completion guard
3. step budget reminders

### Phase C — Replanning + Evidence（P1）

修改：

```text
core/task/model.py
core/task/manager.py
task_update.py
```

增加：

```text
description update
depends_on update
acceptance criteria
completion evidence
```

### Phase D — Task Events / TUI（P1）

增加：

```text
task.created
task.updated
task.completed
```

以及 TUI Task Board。

---

## 7. 最低验收标准

### 依赖历史

```text
Given:
#2 depends_on=[1]

When:
#1 completed

Then:
#2.depends_on == [1]
#2.unresolved_dependencies == []
```

### 状态约束

```text
blocked pending → in_progress
=> rejected
```

```text
pending → completed
=> rejected
```

### 环检测

```text
#1 depends_on=[2]
#2 depends_on=[1]
=> rejected
```

### AgentLoop

```text
LLM end_turn
+
unfinished tasks exist
=> run does not success
```

```text
LLM end_turn
+
all tasks completed
=> success
```

### 简单任务兼容

```text
LLM never creates Task
+
end_turn
=> success
```

### Budget

在接近 `max_steps` 时可观察到明确 reminder，最终阈值仍保留 fail-safe。

### 持久化

进程在写临时文件时被中断，不得破坏已有 `task_N.json`。

---

## 8. 迁移策略

为避免破坏已有 S3 `.tasks/*.json`：

读取时兼容：

```python
depends_on = data.get("depends_on", data.get("blocked_by", []))
```

新写入只使用：

```text
depends_on
```

旧 `blocked_by` 在读取期视为历史依赖，不再执行 `_clear_dependency()`。

建议保留一到两个版本兼容窗口后再删除 legacy parser。

---

## 9. 结果与权衡

### 优点

- 保留 S3 简洁架构；
- Task DAG 可复盘；
- Task 从“LLM 自律”升级为轻量状态机；
- 避免 premature success；
- 降低长任务在 max_steps 附近失控的概率；
- 为未来 Task Board / Scheduler / Subagent orchestration 留出干净接口。

### 成本

- AgentLoop 与 TaskManager 产生轻度耦合；
- TaskManager 测试复杂度增加；
- 旧 `.tasks` 需要兼容迁移；
- 更强状态约束可能暴露现有 LLM 调用中的非法状态转换，需要调整 Tool description / prompt。

### 接受的权衡

当前 LeaveClaude 的目标不是构造完整 workflow engine，而是构造一个稳定、透明、可恢复的 Coding Agent Harness。

因此选择：

```text
Task Tools
    +
Lightweight Task State Machine
    +
Task-aware AgentLoop
```

而不是：

```text
Planner
→ Scheduler
→ Executor
→ Evaluator
→ Workflow Engine
```

---

## 10. 后续 ADR 候选

若后续出现真实需求，再分别设计：

1. **Task DAG 跨 Run / Session Resume**
2. **Task ↔ Subagent Assignment**
3. **Parallel DAG Scheduler**
4. **Hierarchical Parent/Child Task**
5. **Task Verification / Evaluator Agent**
