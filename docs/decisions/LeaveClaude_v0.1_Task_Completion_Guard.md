# LeaveClaude v0.1 独立改造说明：Task Completion Guard

## 0. 改造定位

**目标：**解决 Coding Agent 在已经创建任务计划后，因为 `end_turn`、`max_steps`、`max_tokens` 或模型提前收尾，导致“任务未完成但 Run 被当成成功结束”的问题。

这次改造不追求重构整个 Task 系统，而是做一个**可独立完成、可测试、可演示、可写进简历**的 Harness 级改进。

建议把它作为 LeaveClaude 相比 KamaClaude 的第一个真实独立改造。

---

## 1. 问题背景

在复杂 Coding Task 中，Agent 可能先创建任务计划，例如：

```text
#1 Inspect CLI and current run command
#2 Implement --file support
#3 Add tests
#4 Run full verification
```

但当前 Agent Loop 的成功条件通常主要依赖模型返回：

```text
stop_reason == "end_turn"
```

这会出现一种危险情况：

```text
#1 completed
#2 completed
#3 pending
#4 pending

LLM: “Done.”
stop_reason = end_turn

=> Run success
```

从 Harness 角度看，这个 Run 实际上并没有完成用户目标。

因此需要把：

```text
LLM 认为结束
```

与：

```text
Harness 验证任务确实完成
```

分离。

---

## 2. 改造目标

### 2.1 核心目标

当 Agent 已经创建过 Task 时：

```text
LLM end_turn
        ↓
Task Completion Guard
        ↓
检查 TaskManager
        │
        ├── 全部 completed → success
        │
        └── 存在 unfinished → 不允许 success
                              ↓
                        注入 reminder
                              ↓
                         继续 Agent Loop
```

### 2.2 最终成功条件

Run 只有以下两种情况可以返回 `success`：

```text
A. 本次 Run 从未创建 Task
B. 创建过 Task，且所有 Task 均 completed
```

以下情况不得直接返回 `success`：

```text
存在 pending task
存在 in_progress task
存在 blocked / unfinished task
```

---

## 3. 本次明确不做什么

为了控制 10 月初秋招前的开发范围，本次不要顺手重构整个 Task 系统。

**暂不实现：**

- Task DAG 自动调度
- Parent / Child Task
- Task 与 Subagent 自动绑定
- Task Board TUI
- 跨 Run Task graph resume
- 完整 checkpoint / rollback
- `blocked_by → depends_on` 全量重构
- MCP 改造
- Sandbox
- Canonical History / ContextBuilder 重构

这些继续保留在 ADR Roadmap 中。

本次只解决：

> **“有任务未完成时，Agent 不能错误地宣告成功。”**

---

## 4. 推荐实现设计

### 4.1 TaskManager 增加只读状态查询

建议增加：

```python
class TaskManager:
    def has_tasks(self) -> bool:
        ...

    def unfinished_tasks(self) -> list[Task]:
        ...

    def all_completed(self) -> bool:
        ...
```

参考语义：

```python
def unfinished_tasks(self) -> list[Task]:
    return [
        task
        for task in self.list_all()
        if task.status != "completed"
    ]

def all_completed(self) -> bool:
    tasks = self.list_all()
    return bool(tasks) and all(
        task.status == "completed"
        for task in tasks
    )
```

注意：

```text
无 Task
```

不要由 `all_completed()` 决定是否成功。

“没有创建任务”应由 AgentLoop 单独判断。

---

### 4.2 AgentLoop 注入 TaskManager

推荐：

```python
AgentLoop(
    provider=provider,
    registry=registry,
    bus=bus,
    task_manager=task_manager,
)
```

为了兼容简单任务：

```python
task_manager: TaskManager | None = None
```

---

### 4.3 在 `end_turn` 前增加 Completion Guard

当前逻辑如果类似：

```python
if response.stop_reason == "end_turn":
    return RunOutcome.success(...)
```

改成：

```python
if response.stop_reason == "end_turn":
    if self._can_finish():
        return RunOutcome.success(...)

    reminder = self._build_unfinished_task_reminder()
    context.append_user_or_system_message(reminder)
    continue
```

其中：

```python
def _can_finish(self) -> bool:
    if self.task_manager is None:
        return True

    if not self.task_manager.has_tasks():
        return True

    return self.task_manager.all_completed()
```

---

### 4.4 Reminder 内容必须结构化

不要只说：

```text
You still have work to do.
```

建议：

```text
You attempted to finish the run, but planned tasks are still unfinished.

Unfinished tasks:
- #3 Add tests [pending]
- #4 Run full verification [pending]

Before finishing:
1. inspect the remaining tasks;
2. continue execution or update the plan if it is no longer valid;
3. only finish after all required tasks are completed or explicitly explain why the plan must change.
```

如果已有 `task_list` Tool，可以提醒模型主动调用：

```text
Use task_list if you need the latest task state.
```

---

## 5. 防止 Completion Guard 自己造成死循环

这是本次改造必须考虑的问题。

假设模型连续三次：

```text
end_turn
→ reminder
→ end_turn
→ reminder
→ end_turn
```

不能无限继续。

增加：

```python
max_completion_guard_retries = 2
```

内部：

```python
completion_guard_retry_count = 0
```

逻辑：

```text
第一次 unfinished + end_turn
→ reminder
→ retry_count = 1

第二次
→ reminder
→ retry_count = 2

第三次仍试图结束
→ Run failed
→ reason = unfinished_tasks
```

失败结果应包含：

```text
unfinished task summary
```

例如：

```json
{
  "status": "failed",
  "reason": "unfinished_tasks",
  "unfinished_tasks": [
    {"id": 3, "subject": "Add tests", "status": "pending"},
    {"id": 4, "subject": "Run verification", "status": "pending"}
  ]
}
```

这样 Harness 不会：

```text
无限逼模型继续
```

而是：

```text
有限纠偏 → 明确失败
```

---

## 6. 与 max_steps 的关系

`max_steps` 仍然保留为最终安全阈值。

建议最终语义：

```text
LLM end_turn + no tasks
    → success

LLM end_turn + all tasks completed
    → success

LLM end_turn + unfinished tasks
    → completion guard

completion guard 重试耗尽
    → failed(unfinished_tasks)

step >= max_steps
    → failed(exceeded_max_steps)
```

如果 `max_steps` 触发，同时存在 unfinished tasks，RunOutcome 中附带：

```text
unfinished_tasks
```

方便用户知道：

> Agent 停在哪里，而不是只看到“超过最大步数”。

---

## 7. 可选小增强：Step Budget Reminder

如果当天时间足够，可以增加一个非常轻量的预算提醒。

例如：

```text
80% max_steps:
You have used 80% of the step budget.
Prioritize unfinished planned tasks and verification.
Avoid optional exploration.
```

第一版只做一次提醒即可。

不要现在实现复杂 BudgetManager。

---

## 8. 测试计划

这是本次改造真正有简历价值的关键。

建议至少补以下测试。

### Test 1：简单任务未创建 Task

```text
Given:
- TaskManager empty
- LLM returns end_turn

Then:
- Run success
```

目的：

> Completion Guard 不破坏简单请求。

---

### Test 2：所有 Task 已完成

```text
Given:
#1 completed
#2 completed

When:
LLM returns end_turn

Then:
Run success
```

---

### Test 3：存在 unfinished Task

```text
Given:
#1 completed
#2 pending

When:
LLM returns end_turn

Then:
- Run MUST NOT success
- context receives completion reminder
- AgentLoop continues
```

这是最核心测试。

---

### Test 4：Guard 后完成任务

模拟：

```text
step N:
LLM end_turn
#2 pending

→ Guard reminder

step N+1:
LLM calls task_update
#2 completed

step N+2:
LLM end_turn

→ success
```

证明 Guard 真正允许 Agent 自我修复。

---

### Test 5：模型反复提前结束

```text
#2 pending

LLM end_turn
LLM end_turn
LLM end_turn
```

Then：

```text
Run failed(reason="unfinished_tasks")
```

确保不会死循环。

---

### Test 6：max_steps + unfinished tasks

```text
Given:
unfinished tasks

When:
max_steps reached

Then:
Run failed(exceeded_max_steps)
AND
RunOutcome exposes unfinished task summary
```

---

## 9. 推荐测试方式

不要依赖真实 Claude API。

实现一个 Fake / Scripted Provider：

```python
class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def stream(...):
        return next(self.responses)
```

用预先定义的响应序列测试 AgentLoop：

```python
responses = [
    end_turn_response(),
    tool_call_response("task_update", ...),
    end_turn_response(),
]
```

优点：

- 稳定
- 快
- 不花 token
- 可以构造极端异常
- 更符合测试开发思路

---

## 10. Demo 场景

完成后建议自己跑一个可演示任务：

```text
请为当前项目新增 leave run --file <path>：
1. 检查现有 CLI；
2. 设计方案；
3. 实现参数和文件读取；
4. 补充测试；
5. 运行完整测试验证。
```

人为把：

```text
max_steps
```

设置得偏小，或者用 Scripted Provider 模拟提前 `end_turn`。

### 改造前

```text
#1 completed
#2 completed
#3 pending
#4 pending

Agent: Done.
Run: success
```

### 改造后

```text
Agent: Done.

Harness:
unfinished tasks detected:
#3 Add tests
#4 Run verification

→ continue

最终：
completed → success
或
budget exhausted → failed with unfinished summary
```

这就是非常清楚的 Before / After。

---

## 11. 最低验收标准

只有全部满足，才算这次改造完成：

- [ ] 简单无 Task 请求仍可正常完成
- [ ] 有 Task 时只有全部 completed 才能 success
- [ ] unfinished + end_turn 会触发 reminder
- [ ] Guard 有最大重试次数，不会死循环
- [ ] max_steps 失败结果携带 unfinished task 信息
- [ ] 至少 5 个自动化测试覆盖上述路径
- [ ] `pytest` 全量通过
- [ ] `ruff` / `mypy`（如果项目已有）通过
- [ ] README 增加该特性的设计说明
- [ ] 有一个可复现 Before / After Demo

---

## 12. 建议 Git 提交拆分

不要一个 commit 全塞进去。

建议：

```text
feat(task): expose unfinished task state queries

feat(agent-loop): guard end_turn when planned tasks remain

feat(agent-loop): fail safely after completion guard retries

test(agent-loop): cover unfinished task completion semantics

docs: document task completion guard behavior
```

这样 Git 历史本身也能证明：

> 这不是复制项目后随手改了几行。

---

## 13. README 中建议增加

```markdown
## LeaveClaude Improvements

### Task Completion Guard

KamaClaude-style ReAct agents may attempt to terminate a run while
planned tasks are still pending, especially when working under step/token
budgets.

LeaveClaude adds a Harness-level completion guard:

- `end_turn` is no longer equivalent to unconditional success;
- if a run has created tasks, all required tasks must reach `completed`;
- unfinished tasks trigger a structured reminder and continued execution;
- repeated premature termination fails safely with unfinished-task diagnostics;
- budget exhaustion records remaining task state for debugging and recovery.
```

不要贬低 KamaClaude。

强调：

> LeaveClaude 在学习基线基础上的独立工程改进。

---

## 14. 完成后简历可怎么写

**只有实现并测试完成后再写。**

推荐：

> **任务完成性保障：**针对 Coding Agent 在 Step/Token 预算受限时可能提前结束并遗留未完成任务的问题，在 AgentLoop 引入 Task Completion Guard，将模型 `end_turn` 与 Harness 级成功判定解耦；基于任务状态校验、有限纠偏重试及未完成任务诊断，避免部分执行被误判为成功，并通过自动化测试覆盖提前结束、恢复执行及预算耗尽等异常路径。

这条比：

> “支持 MCP、Multi-Agent、Skill”

更容易证明是你自己的工作。

---

## 15. 面试时的讲法

推荐按四步讲：

### ① 真实问题

> 我在让 Agent 实现 `leave run --file` 时发现，它因为执行预算等原因只完成了一部分修改，但模型已经结束当前回答，原 Harness 会把这种情况当成 Run 结束。

### ② 根因

> 原系统把模型层的 `end_turn` 近似等价于任务层的 success，但模型是否停止生成，并不能证明用户目标已经完成。

### ③ 改造

> 所以我把成功判定上移到 Harness 层：如果 Agent 创建过 Task，就在 `end_turn` 时检查 TaskManager；存在未完成任务则注入结构化 reminder 继续执行，同时设置有限纠偏次数避免死循环。

### ④ 工程验证

> 我使用 Scripted/Fake LLM Provider 构造提前结束、补做任务、重复提前结束和 max_steps 耗尽等路径，通过自动化测试验证成功/失败语义。

这已经是一段很完整的 Agent Harness 系统设计回答。

---

# 推荐实施时间

这次改造应该严格限制在 **0.5～1.5 天**：

```text
2h   阅读调用链并定位 end_turn
2h   TaskManager query + Completion Guard
1h   Guard retry / failure outcome
2h   单元与集成测试
1h   README + Demo + 清理代码
```

如果明显超过 1.5 天：

> 停止扩范围。

你的目标是完成一个**小而完整的独立改造**，而不是在 10 月 8 日前重写 Agent Runtime。
