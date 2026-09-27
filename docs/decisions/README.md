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
