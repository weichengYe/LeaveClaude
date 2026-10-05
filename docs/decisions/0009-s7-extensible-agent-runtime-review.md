# ADR-0009: S7 Extensible Agent Runtime Review

> 阶段复盘：[S7](../note/s7.md)

- **Status**: Proposed / Deferred
- **Stage**: S7
- **Scope**: Skill / Agent Profile / Subagent / Multi-Agent Orchestration / MCP / Runtime Governance
- **Target Project**: LeaveClaude
- **Decision Type**: Architecture Review + Deferred Refactor Plan
- **Date**: 2026-10-02

---

# 1. Context

KamaClaude S7 将系统从“单 Agent + 内置工具”的轻量 Agent Harness，推进到更具扩展性的 Agent Runtime。

S7 的主要增量包括：

1. **Skill**
   - Markdown + Frontmatter 定义工作流；
   - 支持项目级 / 用户级 / 内置三级覆盖；
   - 通过 `system_prompt_override` 改变当前任务行为；
   - 通过 `allowed_tools` 收缩当前 Agent 的工具能力。

2. **Agent Profile**
   - 使用 TOML 定义角色；
   - 当前内置 `planner / executor / reviewer`；
   - Profile 同时包含 role description、system prompt、allowed tools、model。

3. **Subagent**
   - 新增 `spawn_agent` / `agent_result`；
   - Child Agent 使用独立 `ExecutionContext`、`EventBus`、`ToolRegistry`、`TaskManager`；
   - 支持 Foreground 与 Background 两种模式；
   - Child Event 通过 bridge 汇入 Parent EventBus；
   - 支持有限深度的嵌套 delegation。

4. **Multi-Agent Orchestration**
   - `/orchestrate` 通过 Skill Prompt 组合 Planner → Executor → Reviewer；
   - 没有新增专门的 Orchestrator Runtime；
   - 主要依赖 `Skill + Agent Profile + spawn_agent` 组合完成。

5. **MCP**
   - 新增 MCP Client、Server Manager、McpTool Adapter；
   - 支持 stdio 与自定义 TCP transport；
   - 通过 `tools/list` 动态发现工具；
   - 将外部 MCP Tool 包装成内部 `BaseTool` 后注入 `ToolRegistry`。

6. **配套增量**
   - Subagent / Skill lifecycle event；
   - Extended Thinking block 保真；
   - TUI slash completion 与 Subagent tree rendering；
   - transport frame limit、MCP stderr drain 等稳定性补丁。

S7 最有价值的部分不是新增了多少类，而是验证了一种组合式设计：

```text
Skill
+ Agent Profile
+ Subagent
+ Tool Adapter
= Extensible Agent Runtime
```

但 S7 也集中暴露了此前 S4-S6 已经出现的架构问题：

```text
Persistent State
≈
LLM Context

Runner
承担过多 runtime wiring

Child lifecycle
与 Runner lifecycle 不一致

Multi-Agent
缺乏显式 resource governance

Shared Workspace
缺乏 concurrency policy

MCP
协议层自行实现且偏最小原型
```

因此本 ADR 的目标不是逐项照搬 S7，而是明确：

> **哪些抽象应该保留，哪些 wiring 应该重构，哪些能力应该提升为 LeaveClaude 的一级 Runtime Service。**

---

# 2. Decision Summary

总体决策：

> **保留 S7 的组合式抽象，但重构 Context、Lifecycle、Resource、Workspace 与 Protocol Boundary。**

S7 最值得继承的是：

```text
Skill
Agent Profile
Subagent = Tool
MCP Tool Adapter
Hierarchical Event Model
```

S7 最需要重构的是：

```text
Context Projection
Background Task Lifecycle
Subagent Resource Budget
Workspace Concurrency
Workflow State
MCP Protocol Stack
```

---

# 3. Decision Table

| ID | Priority | Decision |
|---|---|---|
| S7-01 | P0 | Skill 保留，但拆分 Instruction / Task / Capability |
| S7-02 | P0 | Canonical Conversation 与 LLM Context 完全解耦 |
| S7-03 | P1 | Agent Profile 保留为 Role + Capability + Model Policy |
| S7-04 | P1 | Agent Profile 的 `model` 字段必须真正生效 |
| S7-05 | P0 | `Subagent = Tool` 作为核心 delegation primitive 保留 |
| S7-06 | P1 | Child 不继承 Parent Conversation，但应继承稳定项目级上下文 |
| S7-07 | P1 | Parent → Child Handoff 使用结构化契约 |
| S7-08 | P0 | BackgroundTaskRegistry 生命周期提升至 Session/Daemon Runtime |
| S7-09 | P0 | Background Subagent 必须增加 Workspace Concurrency Policy |
| S7-10 | P0 | 引入 Hierarchical Agent Resource Budget |
| S7-11 | P1 | Delegation 由 LLM 决策，Runtime 负责 Budget / Depth / Concurrency Guard |
| S7-12 | P1 | Soft Agentic Workflow 与 Hard Workflow 分离 |
| S7-13 | P1 | Planner→Executor→Reviewer 仅保留为一种模板，不固化为核心架构 |
| S7-14 | P1 | MCP Adapter 保留；采用官方 MCP SDK，本地 stdio、远程 Streamable HTTP，废弃自定义 raw TCP |
| S7-15 | P1 | MCP Tool 必须统一进入 Permission / Capability / Trust Boundary |
| S7-16 | P2 | Hierarchical Event / Run Tree 保留并扩展 |
| S7-17 | P1 | Extended Thinking 作为 Provider Protocol Block 透明保留 |
| S7-18 | P1 | 增加组合级 E2E / Lifecycle Test |

---

# 4. S7-01 — Skill 拆分为 Instruction / Task / Capability

## Current Problem

当前 Skill 同时承担 Prompt Template、Tool Whitelist、User Task Rendering。

例如：

```text
/review src/core
```

当前流程近似：

```text
raw input
    ↓
append into thread.jsonl
    ↓
render skill template → goal
    ↓
system_prompt_override = original template
    ↓
runner reads raw thread
```

因此可能同时存在：

```text
Canonical History:
User: /review src/core

Rendered Goal:
完整 Skill Prompt + src/core

System Prompt:
未 render 的 Skill Template
```

问题并非只是 `$ARGUMENTS` 是否正确 replace，而是 Skill Invocation 的语义没有被显式建模。

## Decision

Skill 应显式拆成：

```text
Skill
├── instructions
│   └── 当前 workflow 的行为约束
├── task_template / task_text
│   └── 当前 user turn 的真实语义任务
└── allowed_tools
    └── capability policy
```

例如：

```text
System:
你是一位严格的代码审查员……

Messages[last]:
请审查 src/core

Tools:
read_file
list_dir
bash
```

---

# 5. S7-02 — Canonical History 与 LLM Context 解耦

当前实现非常接近：

```text
thread.jsonl
    ↓
read_messages()
    ↓
context.messages
    ↓
LLM
```

即：

```text
Persistence View
≈
Inference View
```

## Decision

引入显式层次：

```text
User Surface Input
        ↓
InvocationResolver
        ↓
ResolvedInvocation
        ↓
Canonical Store
        +
ContextBuilder
        ↓
LLM Request Context
```

其中：

```text
Canonical Store
记录“真实发生了什么”

ContextBuilder
决定“当前这一轮 LLM 应该看到什么”
```

Skill Invocation 可以保存 raw input，但 LLM 最后一条 User Message 应投影为真正的任务语义。

---

# 6. S7-03 / S7-04 — Agent Profile 成为真正的 Runtime Policy

保留：

```text
AgentProfile
├── role
├── instructions
├── capabilities
└── model_policy
```

当前 `planner / executor / reviewer` 的价值主要体现在不同角色、不同 system prompt、不同 allowed_tools。

但未来 Profile 中的 `model` 一旦存在，就必须形成实际 runtime semantics：

```text
planner
→ model / reasoning policy A

executor
→ model / coding policy B

reviewer
→ model / independent review policy C
```

不要求不同角色必须使用不同模型，但声明必须被 Runtime enforce。

---

# 7. S7-05 — 保留 `Subagent = Tool`

不新增：

```text
PlannerLoop
ReviewerLoop
SubagentLoop
```

统一复用：

```text
AgentLoop
+
ExecutionContext
+
ToolRegistry
```

`spawn_agent` 仅作为 Delegation Primitive。

这是 S7 最值得保留的抽象之一。

---

# 8. S7-06 — Child Context：Cold Conversation + Inherited Stable Context

Child Context 分三类：

```text
Child Context
=
Inherited Stable Context
+
Explicit Handoff
+
Child-local Runtime State
```

### Inherited Stable Context

```text
Global Instructions
Project Instructions
Workspace identity
Security / Permission policy
```

### Explicit Handoff

```text
Goal
Scope
Relevant Context
Constraints
Expected Output
Success Criteria
```

### Non-inherited

```text
Parent Conversation
Parent Tool Trace
Parent TaskManager
Parent transient reasoning
```

核心原则：

> **Cold-start conversation，不等于 cold-start project knowledge。**

---

# 9. S7-07 — Parent → Child Handoff 使用结构化契约

未来可引入：

```python
SubagentHandoff(
    goal="审查权限模块",
    scope=["src/auth/**"],
    relevant_context=[
        "PermissionManager 使用 Future 等待审批",
    ],
    constraints=[
        "不修改 DB schema",
        "只做审查，不修改代码",
    ],
    expected_output="列出问题、文件位置、风险等级",
    success_criteria=[
        "覆盖 permission cache",
        "覆盖 timeout",
        "覆盖 disconnect cleanup",
    ],
)
```

目标：

```text
Context Handoff
≠
Parent History Copy
```

而是：

```text
Task-conditioned Minimum Sufficient Context
```

---

# 10. S7-08 — BackgroundTaskRegistry 生命周期必须提升

当前 `BackgroundTaskRegistry` 创建在 `AgentRunner` 中，但每一次 `SessionManager.send_message()` 都创建新的 Runner。

因此：

```text
Run A
spawn background child X
↓
Run A 结束

User 下一轮
↓
Run B
new AgentRunner
new BackgroundTaskRegistry
↓
agent_result(X)
→ registry 不再包含 X
```

## Decision

`BackgroundTaskRegistry` 至少提升到 SessionRuntime scope，或更高的 Daemon Runtime scope，并显式记录：

```text
session_id
parent_run_id
child_run_id
owner
status
budget
deadline
```

---

# 11. S7-09 — Background Subagent 必须增加 Workspace Concurrency Policy

真正危险的是：

```text
多个 Child
+
共享同一 Workspace
+
WriteFile / Bash
```

可能产生：

```text
覆盖
竞态
不可重复结果
错误 diff
测试相互污染
```

## Decision

V1：

```text
Background Subagent
默认 read-only

Write-capable Child
默认串行
```

后续可以引入：

```text
git worktree
isolated workspace
sandbox branch
merge phase
```

因此未来应有 `WorkspaceManager` 管理 workspace lease、读写模式、隔离、merge、cleanup。

---

# 12. S7-10 — Hierarchical Agent Resource Budget

这是 S7 Subagent 设计必须补充的一项 P0 决策。

## Problem

当前 Child 的：

```text
max_steps
context capability
provider
execution policy
```

基本沿用 Root Agent 默认配置。

这会导致：

```text
Token Explosion
Latency Explosion
Cost Uncertainty
Recursive Delegation Explosion
```

因此 Subagent 必须像 CPU / Memory 一样被 Resource Governance。

## Budget Dimensions

至少区分：

```text
max_total_tokens
max_context_tokens
max_output_tokens_per_call
max_turns
max_tool_calls
timeout_seconds
max_children
max_concurrent_children
max_depth
max_cost
```

并明确：

```text
Model Physical Context Window
≠
Agent Allowed Context Budget
≠
Run Total Token Budget
```

例如：

```text
Model supports:
200K context

Reviewer Child policy:
max_context_tokens = 24K
max_total_tokens   = 30K
max_turns          = 8
timeout            = 60s
```

## Hierarchical Budget Tree

```text
Root Run
100K tokens
180 sec
4 children
2 concurrent
    │
    ├── Child A
    │   30K
    │   60 sec
    ├── Child B
    │   20K
    │   45 sec
    └── Child C
        10K
        30 sec
```

Parent 可以请求预算，但 Runtime 必须最终 enforce：

```text
LLM proposes budget
Runtime grants budget
```

## Budget Reservation

Background 并发时采用：

```text
reserve
→ consume
→ reconcile
→ release unused budget
```

例如：

```text
Root = 100K

reserve A = 30K
remaining = 70K

reserve B = 20K
remaining = 50K

A actual use = 18K
unused = 12K

release 12K
remaining = 62K
```

并发 reserve 必须原子化，避免预算超卖。

## Nested Budget

任何后代预算都受祖先预算约束：

```text
Root 100K
│
├── A 30K
│   ├── A1 15K
│   └── A2 10K
└── B 20K
```

必须满足：

```text
Sum(child reservations)
<=
Parent remaining budget
```

## Preflight Enforcement

每次 LLM Call 前必须进行：

```text
remaining budget
+
estimated input
+
output cap
+
deadline
```

检查。

Provider 返回真实 usage 后再 reconcile。

## Deadline Inheritance

```text
child_deadline
=
min(requested_deadline, parent_remaining_deadline)
```

不能让 Child 获得超过 Parent 剩余生命周期的 timeout。

## Runtime Service

引入：

```text
BudgetManager
├── reserve()
├── consume()
├── reconcile_usage()
├── remaining()
├── release()
└── cancel_on_deadline()
```

以及：

```python
AgentBudget(
    max_total_tokens,
    max_context_tokens,
    max_output_tokens_per_call,
    max_turns,
    max_tool_calls,
    timeout_seconds,
    max_children,
    max_concurrency,
    max_depth,
    max_cost,
)
```

BudgetManager 应成为一级 Runtime Service，而不是塞进 `SpawnAgentTool` 的局部逻辑。

---

# 13. Research / Engineering References for Subagent Budgeting

以下内容不是 KamaClaude S7 原生设计，而是本 ADR 研究阶段用于验证方向的外部参考。

## Pydantic AI Harness

值得重点参考：

```text
per-child usage limit
tree-level usage
timeout
max calls
budget exhaustion outcome
```

与 LeaveClaude 的 Root Budget + Per-Child Budget 方向高度一致。

参考：
- https://pydantic.dev/docs/ai/harness/subagents/

## Microsoft AutoGen

其 termination abstraction 包括：

```text
TokenUsageTermination
MaxMessageTermination
TimeoutTermination
```

适合参考 budget enforcement boundary。

参考：
- https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/termination.html

## Anthropic Multi-Agent Research System

公开实践表明 Multi-Agent 往往通过显著增加 inference-time compute / token 换取性能，因此 Token / Cost 应成为一等 Runtime Resource。

参考：
- https://www.anthropic.com/engineering/multi-agent-research-system

## Self-Resource Allocation in Multi-Agent LLM Systems

研究：

```text
Which Agent?
Which Model?
How Much Budget?
How Long?
```

如何成为 delegation / resource allocation 的一部分。

参考：
- https://arxiv.org/abs/2504.02051

## MaAS / AgentSlimming

相关研究说明 Agent 数量、Agent 类型、LLM calls、Tool calls、Token cost 都可以成为 query-dependent resource allocation 的一部分。

本项目 V1 不采用复杂自适应算法，仅保留为后续 Adaptive Budgeting 研究方向。

---

# 14. S7-11 — Delegation Decision 与 Runtime Guard 分离

当前：

```text
是否 spawn_agent
```

主要由 Parent LLM 决定，这一点保留。

但 Runtime 必须 enforce：

```text
max_depth
max_children
max_concurrency
max_tokens
max_turns
deadline
max_cost
workspace mode
```

原则：

```text
LLM decides what it wants to do
Runtime decides what is allowed
```

---

# 15. S7-12 — Soft Workflow 与 Hard Workflow 分离

S7 `/orchestrate` 的 Planner → Executor → Reviewer 主要写在 Skill Prompt 中，因此属于：

```text
Soft Agentic Workflow
```

特征：

```text
Prompt defines intended topology
LLM chooses actual tool calls
No hard state machine enforcement
```

另一类：

```text
Hard Workflow
```

应由 Code / DAG / State Machine 明确控制。

因此 LeaveClaude 不需要全面迁移到 LangGraph，但必须明确：

```text
Agent Loop
≠
Workflow Runtime
```

二者可以共存。

---

# 16. S7-13 — Planner / Executor / Reviewer 只是 Workflow Template

保留三角色模板，但不固化为 LeaveClaude 核心 Multi-Agent 架构。

未来允许：

```text
Researcher A
Researcher B
Synthesizer
```

或：

```text
Planner
├── Worker A
├── Worker B
└── Worker C
    ↓
Reviewer
```

因此：

```text
Multi-Agent
=
composition capability
```

而不是 fixed 3-agent pipeline。

---

# 17. S7-14 / S7-15 — MCP：保留 Adapter，协议栈采用官方 SDK；本地 stdio，远程 Streamable HTTP

S7 最值得保留：

```text
MCP Tool
→ McpTool Adapter
→ BaseTool
→ ToolRegistry
```

但当前 hand-written JSON-RPC、hard-coded protocol version、自定义 raw TCP、text-only result flattening 更适合作为教学原型，不作为 LeaveClaude 的长期实现方向。

## Decision

LeaveClaude 不自行维护完整 MCP Protocol Stack，而是优先采用官方 MCP SDK：

```text
Official MCP SDK
    ↓
McpAdapter
    ↓
BaseTool
    ↓
ToolRegistry
```

Transport Policy 明确规定：

```text
Local MCP Server
→ stdio

Remote MCP Server
→ Streamable HTTP

Not Adopted
→ custom raw TCP transport
→ newline-delimited JSON-RPC over bare TCP
```

因此未来的 MCP 架构应为：

```text
LeaveClaude
    │
    ▼
Official MCP SDK
    │
    ├── stdio
    │      ↓
    │   Local MCP Server
    │
    └── Streamable HTTP
           ↓
        Remote MCP Server
```

这里需要特别区分：

```text
“远程 MCP 使用 HTTP”
≠
“所有 MCP 都使用 HTTP”
```

本地 Coding Agent 场景下，stdio 仍然适合：

```text
LeaveClaude
→ 启动本地 MCP Server 子进程
→ stdin/stdout 通信
```

远程 MCP 则统一使用标准的 Streamable HTTP，而不是 S7 中的：

```text
asyncio.open_connection()
+
raw TCP
+
newline-delimited JSON-RPC
```

这样可以直接复用官方协议演进能力，并避免 LeaveClaude 自行维护：

```text
protocol version negotiation
remote transport semantics
session handling
HTTP streaming
authentication integration
notifications
cancellation
future MCP protocol changes
```

LeaveClaude 自己真正需要负责的是：

```text
MCP Tool Discovery
McpTool → BaseTool Adapter
Capability Policy
Permission Policy
Trust Boundary
Tool Result Governance
Lifecycle Integration
Context Integration
```

同时必须确保 MCP Tool 与 Builtin Tool 一样统一进入：

```text
Capability Policy
Permission Policy
Trust Boundary
Tool Result Governance
```

外部工具不能因为来自 MCP 就绕过 Harness 的安全与资源治理系统。

---

# 18. S7-16 — Hierarchical Event / Run Tree

保留：

```text
run_id
parent_run_id
```

并扩展：

```text
Session
└── Run
    ├── Child Run
    │   └── Grandchild Run
    └── Child Run
```

用于：

```text
TUI
Tracing
Debug
Cost Attribution
Budget Attribution
Evaluation
Cancellation
```

Budget 也应进入事件系统：

```text
budget.reserved
budget.consumed
budget.exhausted
budget.released
```

---

# 19. S7-17 — Extended Thinking 保持 Provider Protocol Transparency

Provider-specific thinking blocks 应 preserve verbatim，但 AgentLoop 不解释其内部语义。

原则：

```text
Provider-specific protocol block
→ Provider layer preserve
→ Runtime opaque handling
```

---

# 20. S7-18 — Tests 升级为 Compositional / Lifecycle / Resource Correctness

未来重点测试：

```text
Skill Invocation
→ Context Projection
→ LLM request

Background Child
→ Parent Run ends
→ Next Run agent_result still works

Root Budget
→ Child reserve
→ nested reserve
→ release unused budget

Concurrent Children
→ budget no oversubscription

Background write child
→ workspace policy enforced

MCP
→ real MCP server
→ discovery
→ call
→ permission
→ result
```

因此测试目标从：

```text
Local Correctness
```

升级为：

```text
Local Correctness
+
Compositional Correctness
+
Lifecycle Correctness
+
Resource Correctness
```

---

# 21. Proposed LeaveClaude Runtime Architecture after S7

```text
                        User / TUI
                            │
                            ▼
                  Invocation Resolver
                            │
                            ▼
                    ResolvedInvocation
                            │
                            ▼
                     ContextBuilder
                    /      |       \
                   /       |        \
          Instructions   Messages   Capabilities
                   \       |        /
                    \      |       /
                     AgentLoop
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
  Builtin Tool       MCP Tool       spawn_agent
                        │                │
                        ▼                ▼
                  MCP Adapter      Child Runtime
                                         │
                                         ▼
                                  Budget / Workspace
                                     Governance
```

Runtime Services：

```text
Session Store
ContextBuilder
Invocation Resolver
Permission / Policy
Task / Plan
BudgetManager
BackgroundTaskRegistry
WorkspaceManager
Provider Router
Event / Trace
Compaction / Context Governance
MCP Manager
```

---

# 22. Consequences

## Positive

1. Skill 不再和 raw conversation 强耦合。
2. Subagent 保持轻量组合式抽象。
3. Child 可以冷启动，同时不丢失稳定项目指令。
4. Background Agent 生命周期可以跨 Run 管理。
5. Multi-Agent token / latency / cost 变得可预测。
6. Nested Subagent 不再无限放大资源。
7. Workspace 并发风险有明确治理边界。
8. MCP 不再要求 LeaveClaude 自己维护协议演进。
9. Soft Agent Workflow 与 Hard Workflow 的职责清晰。
10. Agent Runtime 从“工具集合”提升为“资源可治理的执行系统”。

## Negative / Cost

1. Runtime Service 数量明显增加。
2. BudgetManager 与 ContextBuilder 会成为高复杂度核心模块。
3. Usage Accounting 需要 Provider 层提供可靠 token / cost 信息。
4. Background Agent cancellation / timeout / cleanup 复杂度增加。
5. Workspace isolation 后续可能引入 git worktree、merge conflict 等工程成本。
6. Profile / Skill / Budget / Permission 多层 policy 需要清晰优先级。
7. 组合测试成本显著增加。

---

# 23. Deferred

以下能力暂不进入第一轮 LeaveClaude 重构：

```text
RL-based Agent Scheduling
Auction-based Resource Allocation
Automatic Multi-Agent Architecture Search
Dynamic Agent Slimming
Adaptive budget learning
Automatic model marketplace routing
Full LangGraph-like durable workflow engine
Distributed multi-machine Agent Runtime
```

V1 只实现：

```text
Static / Parent-requested Hierarchical Budget
+
Runtime Hard Enforcement
```

优先保证：

```text
可解释
可测试
不可超卖
不可绕过
可取消
可观测
```

---

# 24. Final Decision

S7 的最终结论：

> **保留 S7 的组合式扩展思想，但将 Subagent 从“可以启动另一个 Agent”升级为“在明确 Context、Capability、Workspace 与 Resource Envelope 下运行的受控子 Runtime”。**

Subagent 的最终定义应是：

```text
Subagent
=
Isolated Agent Runtime
+
Explicit Handoff
+
Inherited Stable Instructions
+
Scoped Capabilities
+
Scoped Workspace Access
+
Hierarchical Resource Budget
+
Owned Lifecycle
+
Hierarchical Observability
```

而不是：

```text
Subagent
=
“再启动一个拥有同样默认配置的 AgentLoop”
```

---

# 25. Key Takeaway

S7 最值得继承的是：

```text
抽象
```

最需要重构的是：

```text
wiring + governance
```

尤其 Multi-Agent 进入系统以后，以下四项必须成为一级基础设施：

```text
ContextBuilder
BudgetManager
WorkspaceManager
BackgroundTaskRegistry
```

这四个模块共同决定：

```text
Agent 看什么
Agent 能花多少资源
Agent 能修改什么
Agent 能活多久
```

它们比“是否支持更多 Subagent”本身更重要。
