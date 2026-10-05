# ADR 0008：S6 Context Engineering / Compaction / LLM Reliability 架构审查

> 阶段复盘：[S6](../note/s6.md)

- **Status**: Proposed / Deferred
- **Stage**: KamaClaude S6
- **Scope**: Context Engineering、Compaction、Session Persistence、LLM Reliability、Test Architecture
- **Decision timing**: 暂不修改代码；继续完成 S7 后统一落地 LeaveClaude 架构调整

---

# 1. 背景

S6 的主要增量从“Agent 如何执行”转向“Agent 如何管理给 LLM 看的工作上下文”。核心新增能力包括：

1. Global / Project / Session 三层 Context 注入；
2. `context_pct` 上下文占用率；
3. Tool Result 截断；
4. Manual / Auto Compaction；
5. `context.compacted` 事件与 TUI Context Meter；
6. LLM stream retry / backoff；
7. `max_tokens + tool_call` 的协议修复。

S6 已经开始进入真正的 Context Engineering，但当前实现仍把若干不同概念和生命周期混在一起。

---

# 2. 当前源码行为模型

## 2.1 Context 来源

当前 `ExecutionContext.system_prompt()` 将内容按以下顺序拼接：

```text
Base System Prompt
→ Global Context
→ Project Context
→ Session Notes
```

Runner 每个 Run 启动时加载：

```text
~/.kama/context.md
.kama/context.md
session notes
thread.jsonl
```

其中：

```text
~/.kama/context.md
.kama/context.md
```

更准确地应理解为 **Global / Project Instructions**，而不是 Memory。

## 2.2 Session History

每次新的 Session Run：

```text
SessionManager.send_message()
→ append 当前 user message 到 thread.jsonl
→ AgentRunner.run_and_capture()
→ SessionStore.read_messages()
→ 读取完整 thread.jsonl
→ ExecutionContext(prefill_messages=history)
```

因此 `thread.jsonl` 当前同时承担：

1. Canonical conversation history；
2. 下一 Run 的直接 LLM context source。

这是 S6 后续问题的根源之一。

---

# 3. 正面设计：保留

## Decision S6-01：保留 Context Layering 思路

S6 明确引入：

```text
Global
→ Project
→ Session
```

并固定注入顺序。

该方向正确，但语义应重新命名：

```text
Global Context  → Global Instructions
Project Context → Project Instructions
Session Notes   → 混合容器，后续拆分
```

Instruction 的定义：

> 对未来行为具有规范性约束、具有明确 scope 的规则。

而不是“所有长期重要信息”。

## Decision S6-02：保留 Tool Result Truncation，但将其视为局部预算控制

当前只截断 `tool_result`，而不碰普通 text / assistant message，这是合理的局部 Context Budget 策略。

保留原则：

```text
large tool output
→ bounded prefix
→ omitted marker
→ full output 在可审计持久化层保留
```

但它不能替代真正的 Context Selection / Compaction。

## Decision S6-03：保留 Compactor 的失败原子性

当前 Compactor 在摘要失败时不修改 `context.messages`。

原则应保留：

```text
Compaction Failure
≠
Working Context Corruption
```

## Decision S6-04：保留 Provider Protocol Repair

当前对：

```text
stop_reason == "max_tokens"
+
response.tool_calls 非空
```

补充 synthetic `tool_result(is_error=True)`，使：

```text
assistant tool_use
→ user tool_result
```

重新配平。

核心原则：

> Context 不只要语义合理，还必须满足 Provider Message Protocol Invariant。

## Decision S6-05：保留 LLM Stream Retry + Backoff 基线

网络异常进行有限次数 retry + backoff 是合理的，但重试必须进一步区分：

```text
streamed-but-uncommitted UI output
vs
final committed model response
```

---

# 4. 核心问题一：Auto Compaction 只是 Run-local

## 当前行为

Auto Compaction：

```text
AgentLoop
→ Compactor.compact(context)
→ context.messages = [summary, ack]
→ 写 summary_<timestamp>.md
```

但是：

```text
不会调用 SessionStore.write_compacted()
不会重写 thread.jsonl
不会记录 summary 覆盖到哪一条 raw history
```

下一 Run 仍然：

```text
SessionStore.read_messages()
→ 重新读取完整 thread.jsonl
```

因此：

```text
Auto Compaction
=
Run-local Working Context Compaction

≠

Session-level Persistent Compaction
```

## Consequence

如果 raw thread 继续增长并超过模型 hard context limit，则下一 Run 的第一次 LLM 请求可能直接失败。Auto Compaction 没有机会救它，因为当前触发逻辑是：

```text
先 provider.chat()
→ 成功返回 usage.context_pct
→ tool_use
→ 再判断 compact
```

如果第一步已经超限，就根本走不到 compact。

## Decision S6-06 [P0]：Auto Compaction 必须形成 Persistent Context Checkpoint

目标模型：

```text
Canonical Raw History
thread.jsonl
    │ append-only
    ▼

Compaction Checkpoint
summary + compacted_through cursor
    │
    ▼

LLM Active Context
=
active summary
+
uncompacted tail
+
current turn
```

---

# 5. 核心问题二：`prefill_len` 与 Auto Compaction 存在契约冲突

Runner 当前：

```python
prefill_len = len(history)
...
store.append_messages(
    session.id,
    context.messages[prefill_len:],
    run_id=run_id,
)
```

隐含 invariant：

```text
context.messages 在整个 Run 中只能 append
```

但 Compactor：

```python
context.messages = [
    summary,
    ack,
]
```

直接替换整个 working list。

因此：

```text
Runner:
messages = append-only history buffer

Compactor:
messages = replaceable working context
```

存在 **Contract Conflict**。

压缩后如果 `len(context.messages) < prefill_len`，则 `context.messages[prefill_len:]` 可能为空，导致当前 Run 后续生成的新消息无法正确持久化。

## Decision S6-07 [P0]：Persistence Delta 不能由 Working Context 索引推导

未来必须拆开：

```text
Working Context Mutation
≠
Persistence Accounting
```

推荐：

```text
ExecutionContext
├── llm_messages          # 可压缩 / 替换
└── emitted_messages      # 当前 Run 新产生的 canonical messages
```

或由 Session/Run recorder 在消息产生时直接 append canonical event/message。

不要再依赖 `messages[prefill_len:]` 这种脆弱约定。

---

# 6. 核心问题三：Canonical History 与 Inference Context 混合

当前：

```text
thread.jsonl
=
真实历史
+
下一次 LLM 输入
```

Manual Compaction 则直接：

```text
旧 thread.jsonl
→ backup
新 thread.jsonl
→ summary + ack
```

这使“真实发生过什么”和“模型下一次应该看到什么”被同一个文件表达。

## Decision S6-08 [P1]：Canonical History 与 Context View 分离

目标结构：

```text
session/
├── thread.jsonl               # append-only canonical history
├── compactions/
│   ├── cp-001.md
│   └── cp-002.md
├── context_state.json
└── runs/
    └── run-xxx/events.jsonl
```

`context_state.json` 示例：

```json
{
  "active_compaction": "cp-002.md",
  "compacted_through_message": 50
}
```

下一 Run：

```text
cp-002 summary
+
thread[51:]
+
current user message
```

而不是全量 replay。

---

# 7. 核心问题四：Manual Compaction 也需要 Preflight Budget

当前 manual `/compact`：

```text
SessionManager.compact()
→ read_messages() 读取完整 thread
→ compact_messages(messages)
→ 一次性发给同一个 LLM
```

没有 chunking / hierarchical summarization / rolling summary。

因此如果 raw thread 已经超过模型硬上限，`/compact` 本身也可能失败。

## Decision S6-09 [P1]：Compaction 必须发生在 Hard Limit 之前

需要引入：

```text
Preflight Context Budget
```

在 provider.chat 前计算：

```text
effective_input_budget
=
model_window
- output_reserve
- system
- tools
- safety_margin
```

若 active context 已接近预算：

```text
先 compact / select
再调用主 LLM
```

而不是依赖上一次成功调用返回的 `context_pct`。

## Decision S6-10 [P1]：Oversized Compaction 需要 Chunked / Hierarchical Fallback

当 raw history 已无法一次送入 summarizer：

```text
chunk1 → summary1
chunk2 → summary2
...
summary1 + summary2 + ...
→ merged summary
```

或滚动：

```text
S1 + tail1 → S2
S2 + tail2 → S3
```

任何方案都必须保证单次 summarizer input 在 hard limit 内。

---

# 8. Instruction / Memory / State / Context 语义拆分

当前 `session_notes` 可以同时包含：

```text
用户要求这次不要改代码       → Instruction
项目踩过某 API 的坑          → Memory
parser 已完成测试未运行       → State
```

这是一个语义混合容器。

## Decision S6-11 [P1]：明确四类信息

```text
Instruction
= 未来行为应该/不应该怎么做

Memory
= 过去学到、未来值得复用的经验

State
= 当前工作进行到哪里

Context
= 当前这一次 LLM call 实际看到的投影
```

最终：

```text
ContextBuilder
← Instructions
← Memory
← State
← Raw History / Compaction
← Current User
← Tool Results
```

---

# 9. ContextBuilder 必须从 ExecutionContext 中独立出来

当前 `ExecutionContext` 同时承担 runtime state、LLM messages、system prompt 拼接、history prefill、compaction working buffer，职责过重。

## Decision S6-12 [P1]：引入 ContextBuilder

目标：

```text
ExecutionContext
= Runtime Execution State

ContextBuilder
= LLM Context Policy
```

输出：

```text
system
messages
tools
budget metadata
```

---

# 10. Tool Result Truncation 配置接线问题

Config 中已有：

```text
tool_result_limit
tool_result_keep
```

但 `SessionStore.read_messages()` 当前直接：

```python
truncate_tool_results(messages)
```

使用函数默认参数。

因此配置项与实际 read path 存在“看起来可配置，但可能未接线”的问题。

## Decision S6-13 [P2]：配置必须通过明确依赖注入进入 Context Pipeline

推荐由：

```text
ContextBuilder / BudgetPolicy
```

统一持有并应用。

---

# 11. `context_pct` 语义需要更严格

当前大体是：

```text
usage.input_tokens / model_context_window
```

它只是“上一次调用实际输入占模型窗口比例”，不是“下一次调用还剩多少安全预算”。

## Decision S6-14 [P2]：区分 Observed Usage 与 Planned Budget

```text
ObservedContextPct
= 上一次调用实际 usage

PlannedInputBudget
= 下一次调用可安全使用的输入预算
```

不要让一个 `context_pct` 同时承担两个语义。

---

# 12. Stream Retry 的 Commit 语义

当前 retry 可能出现：

```text
Attempt 1
→ token 已推送到 TUI
→ stream 失败

Attempt 2
→ 从头重新生成
→ token 不再推给 TUI
→ final response 以 Attempt 2 为准
```

因此可能：

```text
用户看到的 streamed text
≠
最终真正被 Agent 接受的 response
```

## Decision S6-15 [P1]：UI Streaming 要区分 Speculative 与 Committed

最低要求：

```text
stream.started
stream.aborted
stream.retried
stream.committed
```

Agent 状态只应基于最终 committed response。

---

# 13. 测试缺口

S6 的函数级单测总体不错，但最大问题是缺少：

```text
跨组件 Contract Test
跨 Run Lifecycle Test
```

## Decision S6-16 [P1]：补 Lifecycle Test Matrix

至少覆盖：

| Case | Expected |
|---|---|
| Auto compact intra-run | 下一 LLM step 使用 summary |
| Auto compact persistence | 当前 Run 新消息不能丢 |
| Auto compact cross-run | 明确验证下一 Run 的恢复语义 |
| Manual compact persistence | 下一 Run 使用 compacted context |
| Manual compact backup | raw history 可恢复 |
| Oversized session | 不允许先发送超限请求 |
| Repeated compaction | summary + tail 滚动正确 |
| Stream retry | partial first attempt 不污染 committed state |
| max_tokens + tool call | tool 不执行但 protocol 保持配平 |
| Compaction failure | context / persistence 均不损坏 |

核心测试原则：

> Local Correctness ≠ Compositional Correctness。

---

# 14. 推荐的目标架构

```text
                    ┌─────────────────────┐
                    │  Canonical History  │
                    │    thread.jsonl     │
                    └─────────┬───────────┘
                              │
                    append-only│
                              ▼
                    ┌─────────────────────┐
                    │ Compaction State    │
                    │ summary + cursor    │
                    └─────────┬───────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
Instructions              Memory               State / Plan
        └─────────────────────┬─────────────────────┘
                              ▼
                     ┌─────────────────┐
                     │ ContextBuilder  │
                     │ + BudgetPolicy  │
                     └────────┬────────┘
                              ▼
                    LLMRequestContext
                    system/messages/tools
                              │
                              ▼
                         LLM Provider
```

---

# 15. 文件布局建议

```text
~/.leave/
└── context.md                    # Global Instructions

repo/
├── .leave/
│   ├── context.md                # Project Instructions
│   └── memory/
│       ├── MEMORY.md             # Project Memory
│       └── memory.log.jsonl      # optional provenance log
└── ...

~/.leave/sessions/
└── sess-xxx/
    ├── meta.json
    ├── state.json
    ├── thread.jsonl              # canonical append-only
    ├── context_state.json        # active compaction cursor
    ├── compactions/
    │   ├── cp-001.md
    │   └── cp-002.md
    ├── plans/
    │   └── ...
    └── runs/
        └── run-xxx/
            └── events.jsonl
```

---

# 16. S6 最终结论

S6 的价值不是“加了一个摘要函数”，而是第一次暴露出 Agent Runtime 中真正的 Context Engineering 问题：

```text
Context Source
Context Budget
Context Compression
Context Persistence
Context Protocol Integrity
Context Recovery
```

当前 KamaClaude S6 已经做出不错的第一版，但仍存在三条最关键的架构债：

```text
P0-1  Auto Compaction 仅 Run-local，没有持久化 checkpoint
P0-2  Compactor 替换 messages 与 Runner 的 prefill_len 增量持久化冲突
P1    Canonical History 与 LLM Working Context 没有分层
```

因此 LeaveClaude 后续不应直接复制 S6，而应把：

```text
Persistence
Runtime State
Memory
Instruction
LLM Context
```

拆成独立语义层，再由 ContextBuilder 做最终投影。

---

# 17. 延后到 S7 后统一实施

当前阶段不修改代码。

原因：

1. S7 仍会继续改 Context / Memory / Compaction；
2. 现在修改容易和 S7 再次重复重构；
3. 已经有足够 ADR 记录设计债；
4. 应先完整理解 S1–S7，再做 LeaveClaude 的一次统一架构修正。

进入 S7 后重点验证：

```text
S7 是否真正解决：
- persistent compaction checkpoint
- canonical history / inference view 分离
- ContextBuilder
- session recovery
- memory semantics
- task / plan lifecycle
```

如果没有，再在 LeaveClaude 统一落地。
