# ADR 0006 — S4 Session 引入后的 State / Memory / Context 边界审查

> 阶段复盘：[S4](../note/s4.md)

- **Status**: Proposed / Deferred
- **Stage**: KamaClaude `stage/s4`
- **Scope**: Session、SessionStore、ExecutionContext、AgentRunner、TaskManager、NoteSaveTool
- **Decision timing**: 暂不修改代码；待学习完 S7 后，与各阶段 ADR 一并汇总并统一修正
- **Date**: 2026-09-27

---

## 1. 背景

S4 的核心变化不是单纯增加“聊天模式”，而是引入了一个新的长期生命周期边界：

```text
Session
├── Run 1
├── Run 2
├── Run 3
└── ...
```

在 S3 之前，主要执行关系是：

```text
User Goal
  ↓
Run
  ↓
AgentLoop
  ↓
Finish
```

S4 之后，一个 Session 可以承载多个 Run，并开始持久化：

```text
Session
├── meta.json
├── thread.jsonl
├── notes.md
└── runs/
    ├── run-1/
    ├── run-2/
    └── ...
```

这使 KamaClaude 第一次同时拥有了：

```text
Session State
Conversation History
Agent-written Notes
Run State
Task State
```

因此，S4 是后续讨论 **State / Memory / Context** 边界的关键阶段。

本 ADR 的目的不是立即修改 S4，而是记录：

1. S4 引入 Session 后，系统语义发生了什么变化；
2. 哪些设计是合理的；
3. 哪些边界开始混淆；
4. 哪些问题应在学习完 S7 后统一修正；
5. 对 Task 是否跨 Run 的判断进行明确修正。

---

## 2. 术语约定

为了后续 ADR 保持一致，先固定三个概念。

### 2.1 State

State 表示：

> 当前工作现在进行到了哪里。

典型内容：

```text
session status
current run
plan / task status
active task
blockers
progress cursor
```

State 是当前工作的可恢复状态，不等于长期知识。

---

### 2.2 Memory

Memory 表示：

> 过去得到的、未来可能再次有价值的经验或知识。

对 Coding Agent，更值得长期保存的通常不是容易从仓库重新发现的事实，而是：

```text
architectural rationale
recurring gotcha
failed / rejected approach
non-obvious workflow
historical decision
surprising environment behavior
```

Memory 不应承担 Repository Source of Truth 的职责。

---

### 2.3 Context

Context 表示：

> 某一次 LLM inference 实际看到的 token 集合。

例如：

```text
system prompt
tools
conversation history
current user message
tool results
summary
selected state
selected memory
```

关键原则：

> **Persistent Data ≠ Current Model Context**

保存下来的信息，不应该天然全部进入下一次 LLM 请求。

---

## 3. S4 的主要源码事实

### 3.1 Session 成为跨 Run 生命周期

S4 `SessionManager` 创建 Session，并维护：

```text
id
mode
status
title
created_at
updated_at
run_ids
```

用户每发送一条新消息，就触发一个新的 Run。

因此：

```text
Session
= conversation-level container

Run
= one execution turn / one agent execution instance
```

这是 S4 最重要且合理的架构演进。

---

### 3.2 Conversation History 跨 Run 持久化

S4 将 user / assistant / tool-related message 写入：

```text
thread.jsonl
```

下一次 Run 启动时：

```text
SessionStore.read_messages()
  ↓
ExecutionContext.prefill_messages
  ↓
ExecutionContext.messages
  ↓
Anthropic messages
```

因此，多轮对话第一次真正跨 Run 连续。

---

### 3.3 Session Notes 成为第一版 Agent Memory

S4 增加：

```text
note_save
  ↓
<session>/notes.md
```

其语义是：

```text
Save a concise fact or decision to this session's notes.
These notes are visible in future turns of the same session.
```

也就是说，S4 的 Memory 是：

```text
Session-scoped
Agent-written
Append-only
Markdown-based
```

---

### 3.4 Session Notes 被全文注入 System Prompt

S4 Runner 在每个 Run 开始时读取：

```text
notes.md
```

并传入：

```text
ExecutionContext.session_notes
```

随后 `ExecutionContext.system_prompt()` 构造：

```text
Base System Prompt

## Session Notes
<full notes.md>

Remember important durable facts by calling note_save.
```

因此 S4 实际采用的是：

```text
Memory Store
  ↓
Full Read
  ↓
Always-on System Context
```

---

### 3.5 Task 仍然是 Run-scoped

S4 每次 Run 都创建：

```text
TaskManager(run_path / ".tasks")
```

因此：

```text
Session
├── Run 1
│   └── .tasks/
├── Run 2
│   └── .tasks/
└── Run 3
    └── .tasks/
```

Task 的生命周期仍绑定在 Run，而不是 Session。

---

### 3.6 Session 有持久化，但没有真正 Recovery

虽然磁盘存在：

```text
meta.json
```

且 `SessionStore` 支持 `read_meta()`，但 `SessionManager._get_session()` 只查内存：

```text
self._sessions
```

因此 daemon restart 后：

```text
meta.json still exists
but session is not automatically rehydrated
```

即：

```text
Persistence ≠ Recovery
```

---

## 4. S4 做对了什么

### 4.1 Session / Run 生命周期分离是正确的

S4 最重要的设计价值是：

```text
Session
= multi-turn conversation lifecycle

Run
= one execution lifecycle
```

这是后续 multi-turn agent、context compaction、long-running work 的基础。

此设计应保留。

---

### 4.2 thread.jsonl 作为 append-style conversation persistence 是合理的

JSONL 具备：

```text
simple
append-friendly
human-debuggable
partial-corruption tolerance
```

对于本地 Coding Agent，是非常合适的第一版存储形式。

此方向应保留。

---

### 4.3 notes.md 作为第一版外部记忆并不“过于简单”

S4 使用：

```text
note_save + notes.md
```

这是一个合理 baseline。

问题并不在“文件存储太简单”，而在：

```text
Write Model 合理
Read Model 过于激进
```

即 append-only 文件本身不是主要问题；真正的问题是每次都把全部 Notes 注入 System Prompt。

---

## 5. S4 暴露出的核心架构问题

## 5.1 Durable Conversation 与 LLM Context 被耦合

当前路径：

```text
thread.jsonl
  ↓
read_messages()
  ↓
all messages
  ↓
LLM context
```

这隐含了：

```text
Stored Conversation
≈
Current Model Context
```

短 Session 时问题不大，但 Session 变长后会带来：

```text
context growth
token cost
latency
attention dilution
compaction pressure
```

因此后续应引入：

```text
Persistent Conversation
        ↓
Context Selection / Context Builder
        ↓
Current LLM Context
```

而不是让 storage 直接等于 prompt。

---

## 5.2 Memory 与 Context 被耦合

S4：

```text
notes.md
  ↓
full read
  ↓
system prompt
```

这造成两个问题。

### 问题 A：Context Pollution

低价值、过期、重复 notes 会永久占据 system context。

### 问题 B：Prompt Prefix 不稳定

如果跨 Run 新增 note：

```text
Run N system = Base + Notes v1
Run N+1 system = Base + Notes v2
```

System 变化会破坏更长前缀的稳定性。

因此后续原则应为：

```text
Memory should be persisted independently
and should not automatically become system context.
```

---

## 5.3 Session Notes 的 Scope 不适合真正的 Coding Agent Long-term Memory

S4 Memory：

```text
Session-scoped
```

但 Coding Agent 更有价值的经验通常是：

```text
project-scoped
```

例如：

```text
why this architecture was chosen
which workaround failed before
which generated file should not be edited
which environment issue repeatedly occurs
```

这些信息即使新开 Session，仍然可能有价值。

因此长期方向应考虑：

```text
Session Notes
   ↓
Project Memory
```

但此 ADR 不立即实施。

---

## 5.4 note_save 的写入标准过宽

当前语义：

```text
concise fact or decision
```

对于 Coding Agent 太宽松，容易保存：

```text
current task progress
raw tool output
temporary errors
repo facts easily rediscovered
obvious implementation details
```

后续应收紧为更偏向：

```text
architectural rationale
recurring gotcha
failed approach
non-obvious workflow
historical decision
```

核心原则：

> 能从代码、配置、Git 低成本重新发现的信息，不优先复制进长期 Memory。

---

## 5.5 Session Persistence 没有变成 Session Recovery

S4 已经写了：

```text
meta.json
thread.jsonl
notes.md
```

但 daemon restart 后，SessionManager 仍无法自然恢复旧 Session。

后续可考虑：

```text
lazy hydration
```

例如：

```text
_get_session(sid)
  ↓
not in memory
  ↓
meta exists on disk?
  ↓ yes
read_meta()
rehydrate session
rebuild lock
```

这是 State 系统比 Memory 系统更优先的可靠性问题之一。

---

## 6. 关于 Task 是否跨 Run：修正后的结论

这是本 ADR 最重要的设计修正之一。

### 6.1 不采用“Task 直接提升到 Session scope”

此前曾考虑：

```text
Run-scoped Tasks
   ↓
Session-scoped Tasks
```

现在认为这种改法过于粗糙。

原因是：

> Session 是一段持续对话，而用户目标可能在不同 Run 之间不断变化。

例如：

```text
Run 1:
实现自动 Memory Retrieval

Run 2:
先不要实现，重新讨论 BM25 是否合理

Run 3:
决定改成 filesystem JIT
```

如果整个 Session 只有一套 Task List，就会出现：

```text
stale task
goal drift
wrong pending work
unclear cancellation
unclear replanning
```

因此：

> **Task 不应该直接 Session 化。**

---

## 6.2 Task 属于 Planning State

Task 的本质不是基础 Session State，而是：

```text
goal-dependent mutable planning state
```

它依赖当前 Goal，因此用户改变目标时，Task 可能需要：

```text
revise
cancel
supersede
split
merge
reorder
replace
```

因此 Task 应被视为：

```text
Planning State
```

而不是不可变的 Session State。

---

## 6.3 后续如果支持跨 Run 长任务，应增加 Plan / Work abstraction

更合理的长期结构：

```text
Session
│
├── Conversation
├── Session State
└── Plans
    ├── Plan A
    │   ├── Work Task 1
    │   └── Work Task 2
    └── Plan B
        └── ...
```

关系：

```text
Plan
  ↓ may span
Run 1
Run 2
Run 3
```

而不是：

```text
Session
  ↓ owns one global task list
```

---

## 6.4 区分 Work Task 与 Execution Task

后续建议区分两类 Task。

### Work Task

用户真正关心的长期工作单元：

```text
实现 Session Recovery
增加 Prompt Cache
重构 Memory
```

可以跨 Run。

### Execution Task

Agent 为当前 Run 临时拆出的执行步骤：

```text
read manager.py
grep references
modify function
run pytest
run lint
```

应保持 Run-scoped。

因此可能形成：

```text
Plan
└── Work Task
    ├── ephemeral execution task
    ├── ephemeral execution task
    └── ephemeral execution task
```

---

## 6.5 跨 Run Task 需要 Replanning，而不只是 status update

当前 KamaClaude TaskStatus：

```text
pending
in_progress
completed
```

对于单 Run 已足够。

如果未来支持 durable Plan，至少应考虑：

```text
pending
in_progress
completed
blocked
cancelled
```

必要时增加：

```text
superseded
```

用户改变方向时：

```text
old task != failed
old task != completed
```

更可能是：

```text
cancelled / superseded
```

这属于 replanning semantics。

---

## 7. S4 后续应建立的目标边界

最终希望形成：

```text
Repository / Git
= source of truth

Instructions
= must-follow stable rules

State
= current work progress

Memory
= reusable historical experience

Context
= current inference view
```

并明确：

```text
State ≠ Memory
Memory ≠ Context
Persistence ≠ Context Injection
Session ≠ Context Window
Task ≠ Session
```

---

## 8. 建议的长期数据生命周期

### Project scope

```text
Project Instructions
Project Memory
Repository / Git
```

### Session scope

```text
Conversation History
Session State
Plans
```

### Plan scope

```text
Goal
Revision
Work Tasks
Dependencies
```

### Run scope

```text
ExecutionContext
steps
events
execution tasks
tool outputs
```

---

## 9. S4 阶段暂不实施的修改

由于当前学习路线仍需继续完成 S5–S7，本 ADR 明确：

**S4 阶段不修改现有代码。**

以下均记录为 post-S7 consolidation candidates：

```text
1. Session restart recovery
2. Session Notes → Project Memory
3. Notes 从 System Prompt 移除
4. 收紧 note_save 写入规范
5. Persistent thread 与 LLM context view 分离
6. 引入 ContextBuilder
7. Task 保持 Run-scoped 默认语义
8. 如需跨 Run 长任务，再引入 Plan / Work abstraction
9. 区分 Work Task 与 Execution Task
10. 为 durable plan 增加 replanning / cancel / supersede 语义
```

---

## 10. 不在 S4 ADR 中提前决定的事项

以下问题需要结合 S5–S7 的实际实现后再决定：

```text
Memory 是否需要 consolidation
Memory 是否需要 index / search
是否需要 project memory log + curated view
Compaction 如何与 raw thread 分离
ContextBuilder 的准确职责
Prompt Cache 如何与动态 Context 对齐
Plan 是否需要单独 PlanManager
Task DAG 如何保存历史结构
Session restore 的具体策略
```

尤其不在 S4 阶段提前引入：

```text
Vector DB
Embedding Retrieval
Automatic Memory RAG
Knowledge Graph
Structured Profile
```

避免在尚未看完 S7 前过度设计。

---

## 11. 当前阶段的决策

### Decision 1 — 保留 Session → Runs 结构

S4 的 Session / Run 生命周期分离是正确方向。

```text
Session = multi-turn container
Run = one execution instance
```

---

### Decision 2 — 保留 thread.jsonl 作为持久化 baseline

但记录后续需要：

```text
Stored History
≠
Rendered LLM Context
```

---

### Decision 3 — 保留 note_save + file-based memory 的思想

不因为其简单而立刻换成复杂 Memory Framework。

但：

```text
Session Notes → System Prompt
```

被标记为后续重点审查对象。

---

### Decision 4 — 不直接把 Task 提升到 Session scope

当前 Run-scoped TaskManager 保持不变。

未来若需要跨 Run 工作，应优先考虑：

```text
Plan / Work
   ↓
Work Tasks
   ↓
Runs
```

而不是：

```text
Session
   ↓
One Global Task List
```

---

### Decision 5 — 暂不修改代码

本 ADR 仅用于记录设计判断。

待 S7 学习完成后：

```text
S3 ADR
S4 ADR
S5 ADR
S6 ADR
S7 ADR
   ↓
Unified Architecture Review
   ↓
LeaveClaude consolidated correction
```

再统一确定实际重构范围。

---

## 12. Post-S7 统一审查时必须回答的问题

完成 S7 学习后，统一回顾以下问题：

1. Session 到最终版本是否真正支持 restart recovery？
2. Task 到 S7 是否仍为 Run-scoped？如果是，是否仍需要 Plan abstraction？
3. Session Notes 到 S7 是否仍为 session-scoped append-only？
4. Notes 是否仍然全文进入 System Prompt？
5. Global / Project context 是否应视为 Instructions 而不是 Memory？
6. Conversation Compaction 是否覆盖/改变 canonical history？
7. Persistent thread 是否仍直接等于下一次 LLM Context？
8. S7 是否已经出现 ContextBuilder 类似抽象？
9. Prompt Cache 是否支持 growing messages 的 moving cache？
10. 是否真的出现了足够大的 Memory corpus，值得增加 search / retrieval？
11. Task 是否需要区分 durable Work Task 与 ephemeral Execution Task？
12. 如果引入跨 Run Plan，用户改变目标时如何 revise / cancel / supersede？

---

# Final Takeaway

S4 最值得保留的不是某个具体实现，而是它第一次建立了：

```text
Session
  ↓
Multiple Runs
```

并因此暴露了一个后续 Coding Agent Harness 必须认真处理的问题：

```text
Persistent State
Persistent Memory
Persistent Conversation

        ≠

Current LLM Context
```

对 Task 的最终判断也应记录为：

```text
Memory
→ Project-scoped
→ 默认跨 Session

Plan / Work Task
→ Goal-scoped
→ 可以跨 Run
→ 必须支持 replanning

Execution Task
→ Run-scoped
→ 默认不跨 Run
```

因此，S4 阶段不应简单把 TaskManager 从 `run/.tasks` 移到 `session/.tasks`。
真正合理的跨 Run 方案，应在完成 S7 学习后，基于整个系统最终能力决定是否增加独立的 **Plan / Work abstraction**。
