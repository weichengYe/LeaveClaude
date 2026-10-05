# S0–S7 阶段复盘

这里是在复现 KamaClaude `S0`–`S7` Agent Runtime 的过程中，逐阶段写下的复盘。

每篇的组织方式：

- **理解**——该阶段的核心机制与数据流，尽量做到能自己画出来
- **发现 / 思考**——当时对架构缺陷或设计取舍的判断
- **遗留**——尚未解决、留给后续阶段或 ADR 的问题

这些结论性内容已提炼为 [`../decisions/`](../decisions/) 下的 ADR。**两者冲突时以 ADR 为准。**
其中一部分已在 v0.1 落地（例如 Task Completion Guard），其余仍处于 Deferred 状态，待统一修正。

| 复盘 | 主题 | 对应 ADR |
|---|---|---|
| [S0](s0.md) | 守护进程 / CLI 的通信分层、JSON-RPC、NDJSON | [0003](../decisions/0003-s0-followups.md) |
| [S1](s1.md) | 单进程 Agent 闭环与事件广播 | — |
| [S2](s2.md) | core 与 CLI 的正式连接、broadcaster 背压 | [0004](../decisions/0004-s2-review.md) |
| [S3](s3.md) | Trace 埋点与自主规划 | [0005](../decisions/0005-s3-review.md) |
| [S4](s4.md) | Session 引入与 state / memory / context 边界 | [0006](../decisions/0006-s4-session-state-memory-context-review.md) |
| [S5](s5.md) | Permission 审批模型与消息泵死锁 | [0007](../decisions/0007-s5-permission-tool-execution-review.md) |
| [S6](s6.md) | 三层 context、自动 / 手动压缩的边界 | [0008](../decisions/0008-s6-context-engineering-review.md) |
| [S7](s7.md) | Skill / Subagent / MCP 的实现缺陷盘点 | [0009](../decisions/0009-s7-extensible-agent-runtime-review.md) |
