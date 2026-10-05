# LeaveClaude

本地 AI Agent 系统。`leave-core` 作为常驻守护进程处理所有任务，`leave`（CLI）和 `leave-tui`（TUI）通过 TCP loopback 与之通信。

## Project Origin

LeaveClaude is a learning and refactoring project based on
[KamaClaude](https://github.com/youngyangyang04/KamaClaude).

I reproduced the S0-S7 Agent Runtime incrementally, reviewed its
architecture through ADRs, and then developed independent improvements
around agent reliability, context governance and runtime design.

The first independent improvement in LeaveClaude v0.1 is
**Task Completion Guard** (see below).

## 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | macOS / Linux |
| Python | 3.12.x |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 |

安装 uv（若尚未安装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 由 uv 自动管理，无需手动安装。

## 快速开始

```bash
git clone <repo> && cd LeaveClaude
uv sync
cp .env.example .env        # 按需修改

uv run leave-core &          # 启动守护进程（后台）
uv run leave ping            # 验证连通：应返回 pong
uv run leave --version       # 应输出 0.0.1
```

## 文档

- **[RUNBOOK.md](./RUNBOOK.md)** — 完整操作参考：配置、开发命令、故障排查
- **[WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)** — IPC 协议定义（由代码生成，勿手动编辑）

## LeaveClaude Improvements

### Task Completion Guard

KamaClaude-style ReAct agents may attempt to terminate a run while planned
tasks are still pending, especially when working under step or token budgets.
The harness previously treated `end_turn` as unconditional success, so a
partially executed task could be reported as done.

LeaveClaude adds a Harness-level completion guard:

- `end_turn` 不再等价于无条件成功；
- 如果本次 run 创建过 Task，则所有 Task 必须达到 `completed` 才可 success；
- 存在未完成任务时注入结构化 reminder，并继续 Agent Loop；
- 连续提前收尾耗尽有限重试次数后，以 `failed(unfinished_tasks)` 安全失败；
- `max_steps` 耗尽时同样在结果中附带剩余任务状态，便于调试与恢复。

判定语义：

```text
end_turn + 未创建 Task          → success
end_turn + Task 全部 completed  → success
end_turn + 存在 unfinished Task → 注入 reminder 后继续执行
Guard 重试耗尽                  → failed(unfinished_tasks)
step >= max_steps               → failed(exceeded_max_steps) + unfinished_tasks
```

实现位置：`TaskManager.has_tasks/unfinished_tasks/all_completed`、`AgentLoop` 的
完成性守卫、`RunOutcome.unfinished_tasks` 与 `run.finished` 事件字段。

离线可复现的 Before / After 演示：

```bash
uv run python scripts/demo_completion_guard.py
```

设计说明见 [docs/decisions/LeaveClaude_v0.1_Task_Completion_Guard.md](./docs/decisions/LeaveClaude_v0.1_Task_Completion_Guard.md)。
