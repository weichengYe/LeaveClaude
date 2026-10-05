# 学习笔记（S0–S7）

这里是在复现 KamaClaude `S0`–`S7` Agent Runtime 过程中逐阶段写下的笔记。

- 写作时点是**读完该阶段代码之后**，目的是记录自己的理解与疑问，因此保留了口语化的
  打卡体与当时的思考顺序，未做事后润色。
- 每篇的结论性内容已提炼为 [`../decisions/`](../decisions/) 下的 ADR。**两者冲突时以 ADR 为准。**
- 笔记中标注「发现 / 思考」的部分是当时对架构缺陷的判断：一部分已在 v0.1 落地（例如
  Task Completion Guard），其余仍处于 Deferred 状态，待统一修正。

| 笔记 | 主题 | 对应 ADR |
|---|---|---|
| [s0](s0.md) | 守护进程 / CLI 分层、JSON-RPC、NDJSON、ping | [0003](../decisions/0003-s0-followups.md) |
| [s1](s1.md) | Runner / EventBus / AgentLoop 的数据流 | — |
| [s2](s2.md) | core 与 CLI 的连接、broadcaster 背压 | [0004](../decisions/0004-s2-review.md) |
| [s3](s3.md) | Trace 埋点、plan-as-tool、事务式回滚设想 | [0005](../decisions/0005-s3-review.md) |
| [s4](s4.md) | Session 概念、state / memory / context 三分 | [0006](../decisions/0006-s4-session-state-memory-context-review.md) |
| [s5](s5.md) | PermissionManager、审批死锁排查 | [0007](../decisions/0007-s5-permission-tool-execution-review.md) |
| [s6](s6.md) | 三层 context、自动 / 手动压缩、run 间摘要断层 | [0008](../decisions/0008-s6-context-engineering-review.md) |
| [s7](s7.md) | Skill / Subagent / MCP 的实现缺陷盘点 | [0009](../decisions/0009-s7-extensible-agent-runtime-review.md) |
