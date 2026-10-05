# Architecture Decisions

记录对项目产生持续影响的决策,采用 ADR (Architecture Decision Records) 格式。每篇 ADR 包含:

- **背景**: 当时遇到的问题或机会
- **决策**: 选了什么方案
- **取舍**: 为什么不选其他方案
- **影响**: 这条决策会如何约束后续代码

## 索引

| 编号 | 标题 | 状态 | 日期 |
|---|---|---|---|
| [0003](0003-s0-followups.md) | S0 阶段的两个跟进改进(JSON-RPC id 类型 / start() 探测顺序) | 已采纳 | 2026-09-21 |
| [0004](0004-s2-review.md) | S2 传输层与运行时健壮性审查(broadcaster drain timeout / RPC 超时 / Run 监督)——待 S7 后实施 | 待实施 | 2026-09-23 |
| [0005](0005-s3-review.md) | S3 Task Planning 系统强化——保留任务 DAG、收紧状态约束并让 AgentLoop 感知任务状态 | Proposed | 2026-09-26 |
| [0006](0006-s4-session-state-memory-context-review.md) | S4 Session 引入后的 State / Memory / Context 边界审查 | Proposed / Deferred | 2026-09-27 |
| [0007](0007-s5-permission-tool-execution-review.md) | S5 Permission / Tool Execution / Human-in-the-loop 安全边界审查 | Proposed / Deferred | 2026-09-27 |
| [0008](0008-s6-context-engineering-review.md) | S6 上下文工程 / 压缩 / LLM 可靠性架构审查 | Proposed / Deferred | 2026-10-02 |
| [0009](0009-s7-extensible-agent-runtime-review.md) | S7 可扩展 Agent Runtime 架构审查(Skill / Agent Profile / Subagent / MCP) | Proposed / Deferred | 2026-10-02 |

## v0.1 独立改造

ADR 0003–0009 是对 KamaClaude S0–S7 各阶段的架构审查，多数处于 Deferred 状态。以下是从
LeaveClaude 自身出发、已落地实现的独立改造：

| 文档 | 标题 | 状态 | 日期 |
|---|---|---|---|
| [Task Completion Guard](LeaveClaude_v0.1_Task_Completion_Guard.md) | v0.1 独立改造：任务完成性守卫——end_turn 不再等价于成功 | 已实现 | 2026-10-02 |

## 相关文档

- [../note/](../note/) — S0–S7 各阶段的学习笔记，是上述 ADR 的原始推导过程
