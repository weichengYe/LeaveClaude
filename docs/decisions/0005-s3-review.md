# ADR 0005: S3 阶段的四个跟进改进（任务系统 / DAG 审计 / 测试隔离 / LLM 保险丝）

## 状态
已审查，**待实施**（计划与 ADR 0004 一起，在 S7 学完后统一实施，届时对照本 ADR 执行）

## 背景
S3 同步完成后，实际跑通了两次真实任务（一次 15 步失败、一次 45 步成功），并做了端到端复盘，包括：

- 读 `runs/<run_id>/events.jsonl` 的完整事件流（342 条事件）
- 逐个检查 `runs/<run_id>/.tasks/task_*.json` 的落盘状态
- 追踪 LLM 在 step 11 调 `task_create` 时的 `tool.call_started` params
- 对比 `git stash` 前后的集成测试结果（隔离 pre-existing 问题）

本 ADR 记录**复盘发现的 4 个真实问题**，均为"当前能跑、S3 阶段不可见、S4+ 会放大"的隐患。不修代码，等 S7 学完后统一改。

---

## 问题 1: `TaskManager._clear_dependency()` 会抹平 DAG 审计信息

### 现状
`src/leave_claude/core/task/manager.py:79-80` 的 `update()` 方法：

```python
if status == "completed":
    self._clear_dependency(task_id)
```

以及 `manager.py:107-120` 的 `_clear_dependency()`：

```python
# 将 completed_id 从所有其他任务的 blocked_by 列表中移除
def _clear_dependency(self, completed_id: int) -> None:
    for f in self._dir.glob("task_*.json"):
        try:
            data = json.loads(f.read_text())
        except (ValueError, json.JSONDecodeError):
            continue
        blocked = [int(x) for x in data.get("blocked_by", [])]
        if completed_id in blocked:
            data["blocked_by"] = [x for x in blocked if x != completed_id]
            data["updated_at"] = _now()
            f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
```

### 实际运行证据
`runs/20260925-144950-d85a79/`（45 步成功 run）的时序：

**T0** — LLM 在 step 11 发起 4 个 `task_create`，其中 task 2 传了 `"blocked_by": [1]`（tool.call_started 事件里可见）：

```json
{"type": "tool.call_started", "tool_name": "task_create",
 "params": {"subject": "Implement argparse --file + wire into cmd_run",
            "description": "Add --file/--goal mutually exclusive args...",
            "blocked_by": [1]}}
```

`task_create` 工具返回（`tool.call_finished` 的 output 字段）也正确显示了 DAG：

```json
{"id": 2, ..., "blocked_by": [1], "status": "pending", ...}
```

**T1** — task 1 完成时（step 14，`task_update {task_id:1, status:"completed"}`）：

```python
# _clear_dependency(1) 扫描所有 task_*.json
# 把 1 从 task_2.blocked_by 移除
# → 磁盘上的 task_2.json: blocked_by = []
```

**T2** — run 结束后查看 `.tasks/task_2.json`：

```json
{"id": 2, "status": "completed", "blocked_by": []}  ← 依赖关系被抹平
```

### 问题
**事后审计失败**。`events.jsonl` 保留了原始 DAG（`tool.call_started` params 里能看到 `"blocked_by": [1]`），但 `.tasks/*.json` 只保留了"全空依赖 + 全部 completed"的终态。看 JSON 文件的人无法回答"这次 run 中 task 3 究竟依赖谁"。

### 决策
**接受现状，不改代码**——理由：
- Agent 运行时视角下 `_clear_dependency` 是有用的（简化了 LLM 维护反向引用）
- 审计可以从 `events.jsonl` 重建（`tool.call_started` params 保留了完整的 DAG 定义）
- 改成"软删除 / 历史快照"会引入新的 schema 复杂度，S3 阶段不值得

**但记录设计约束**：`.tasks/*.json` 是**运行时快照**，不是**审计源**。审计必须从 `events.jsonl` 重建。

### 影响后续
S4+ 如果引入 sub-agent / 多 worker 并行执行，DAG 语义会从"顺序提示"变成"调度约束"。届时 `blocked_by` 的清空策略需要重新设计（可能要改成在 task 侧加 `unblocked_at` 时间戳而不是删除依赖）。

---

## 问题 2: task 系统没有暴露 JSON-RPC handler，S3 集成测试 5 个失败

### 现状
`src/leave_claude/core/app.py:153-155` 的 `CoreApp` 只注册了三个 handler：

```python
server.register("core.ping", self._ping_handler)
server.register("agent.run", self._agent_run_handler)
server.register("event.subscribe", self._subscribe_handler)
```

没有 `task.create` / `task.list` / `task.start` / `task.cancel` 的注册。

`src/leave_claude/core/bus/commands.py` 也没有对应的 `TaskCreateCommand` / `TaskListCommand` 等 pydantic 模型（grep 确认：只有 `PingCommand` / `AgentRunCommand` / `EventSubscribeCommand`）。

而 `tests/integration/test_s3_task_graph.py` 的 5 个测试**全部通过 wire 协议直接调 daemon**：

```python
resp = await _send_recv(reader, writer, "task.create", ...)
# → {"error": {"code": -32601, "message": "Method not found: task.create", ...}}
```

### 实际运行证据
- `uv run pytest tests/integration -q` → **5 failed, 7 passed**
- `git stash push -u` 回到改动前基线，**得到完全相同的 5 个失败**
- 结论：这是 S3 commit 里的既存 gap，与 `--file` 功能无关

### 设计意图判断
S3 commit message 明确说"**移除全部 CLI task 子命令，任务管理完全由 Agent 自主通过工具完成**"。测试里的 `task.create`/`task.start`/`task.cancel` 是模拟"外部客户端通过 daemon 通信"，但这个 wire 协议在 S3 设计里就不该存在（task 是 Agent 的私有空间，不是公共 API）。

### 决策
**保留 5 个失败测试，不修 daemon**——理由：
- S3 的哲学是"task 为 Agent 服务，不为人服务"
- 修 daemon 意味着要在 `bus/commands.py` 加 4+ 个模型类，反而违背 S3 设计意图
- 这 5 个测试可以标记为 `@pytest.mark.skip(reason="S3 spec gap: task.* wire protocol intentionally not implemented")` 或移到 `tests/pending/` 目录

**但记录两个未完成方向**：
1. 若 S4+ 引入"跨 run 任务管理"（人可以用 CLI 查看/干预任务），此时才需要在 wire 层加 `task.*` 协议
2. 若保留 `test_s3_task_graph.py` 作为 S4 规划参考，需要在测试文件顶部加 `pytest.skip` 标记，避免 CI 假阳性

### 影响后续
- 目前 `pytest tests/integration` 结果是 **7 passed, 5 failed**，不是 clean green
- CI/CD 若启用会红——需要在 CI 层面用 `pytest --deselect` 或 `xfail(strict=False)` 标记这 5 个

---

## 问题 3: 集成测试 fixture 污染真实 `runs/` 目录

### 现状
`tests/conftest.py` 的 `running_daemon` fixture 起一个真实 daemon，但**没有把 daemon 的 runs_dir 指到 tmp**：

```python
env = os.environ.copy()
env["LEAVE_PORT"] = str(free_port)
env["LEAVE_LOG_FILE"] = ""
env["LEAVE_LOG_LEVEL"] = "WARNING"
proc = subprocess.Popen([sys.executable, "-m", "leave_claude.core"], env=env)
```

测试里发出的 `agent.run`（`tests/integration/test_s2_dual_process.py:33,79,118`）用的是真实 runs_dir（默认 `./runs/`）：

```python
result = await client.send_command("agent.run", {"goal": "hello"})
await client1.send_command("agent.run", {"goal": "broadcast test"})
await client1.send_command("agent.run", {"goal": "replay test"})
```

### 实际运行证据
我前一天清了 `runs/` 目录，跑 3 轮集成测试又产生了 33 个测试 run（goal 是 `"hello"` / `"broadcast test"` / `"replay test"`），全部 `failed`，1-2 步就结束。

### 问题
- **真实 run 历史被测试垃圾污染**——想查一次真人 run 得在一堆 `hello` 里翻
- 测试 run 还会**真调用 LLM**（`hello` 也要走 Anthropic API），浪费 token
- `runs/` 目录没有被 `.gitignore` 屏蔽（实际被屏蔽了——commit 里看不到），但**依然占磁盘**，累积会越来越大

### 决策
**需要修**——最低成本方案是在 `running_daemon` fixture 里加一个环境变量：

```python
env["LEAVE_RUNS_DIR"] = str(tmp_path / "runs")  # ← S3 还没实现这个环境变量
```

对应需要在 `src/leave_claude/core/config.py` 加 `LEAVE_RUNS_DIR` env var 支持（如果 S3 没有）。若实现成本高，备选方案是**集成测试只调 `task.*` 类测试，不真调 `agent.run`**（但这会丢失对 run 链路的覆盖）。

### 影响后续
- S4+ 若加入"清理老 run"功能（如 `leave core cleanup --older-than 30d`），应该能识别"测试产生的 run"（可通过 goal 前缀 `hello` / `broadcast test` / `replay test` 识别，但这很 hacky）
- 更干净的做法是 daemon 启动时读取 `LEAVE_RUNS_DIR`，测试 fixture 把它指到 tmp 目录

---

## 问题 4: `max_tokens=4096` 硬编码 + `stop_reason=max_tokens` 时直接 failed

### 现状
`src/leave_claude/core/llm/provider.py:63`：

```python
"max_tokens": 4096,
```

`src/leave_claude/core/loop.py:72-77`：

```python
# Termination check — end_turn wins over max_steps if both hit on same step
if stop_reason == "end_turn":
    context.mark_succeeded()
elif context.step >= context.max_steps:
    context.mark_failed("exceeded_max_steps")
```

没有对 `stop_reason == "max_tokens"` 的显式处理——**响应被截断时 tool_use JSON 不完整**，Anthropic SDK / provider 校验失败，异常往上冒，被 `loop.py` 捕获成 `llm_error`。

### 实际运行证据
`runs/20260925-140449-9751ad/`（15 步失败的 run）：

```json
{"type": "llm.usage", "output_tokens": 4096}      ← step 23 踩满
{"type": "run.finished", "status": "failed", "reason": "llm_error", "steps": 15}
```

同一 run 里 step 21/22 的 `output_tokens = 3383 / 3364`（接近满）。

后续（2026-09-25 下午）临时修法是**直接把 `max_tokens` 提到 16384**（工作区改动，未 commit 到 S3 commit 里），但没有做 `stop_reason == "max_tokens"` 的显式处理，只是把问题推迟到 16384 触发的时候。

### 问题
- **4096/16384 都只是推迟**，任务足够复杂时终会撞上限
- DeepSeek 的 `finish_reason=length`（OpenAI 风格）→ Anthropic 的 `stop_reason=max_tokens` 映射可能在 provider 层丢掉
- LLM 被截断时丢失的往往是最关键的 tool_use JSON——直接 failed 浪费了前面 N 步积累的工作

### 决策
**需要修，但分两层**：
1. **Provider 层**：`max_tokens` 从硬编码改成 `LEAVE_LLM_MAX_TOKENS` 环境变量 / `llm.max_tokens` TOML 配置（跟随 `max_steps` 的四层优先级）
2. **Loop 层**：`stop_reason == "max_tokens"` 时不直接 failed，而是：
   - 丢弃截断的 tool_use block
   - 向 LLM 注入一条 system 提示："上次回复因达到 max_tokens 被截断，请缩短输出或分步调用工具"
   - 让 LLM 重试一次（最多 2 次，之后才 failed）

### 影响后续
- S4+ 引入 sub-agent / 更复杂的任务编排后，单步响应会更容易踩 4096/16384 上限
- S6 引入 cost_budget router 后，`max_tokens` 也是预算控制的一部分，必须可配置

---

## 总结：4 个问题的优先级

| 编号 | 问题 | 严重度 | 修法 | 时机 |
|---|---|---|---|---|
| 1 | `_clear_dependency` 抹平 DAG 审计 | 低 | 接受现状；审计走 events.jsonl | 不修 |
| 2 | task 系统无 wire 协议 → 5 个集成测试失败 | 中 | skip 标记 / xfail；真修等到需要跨 run 任务管理时 | S4+ |
| 3 | 集成测试污染真实 `runs/` | 中 | `LEAVE_RUNS_DIR` env var 指向 tmp | **S4 前必修** |
| 4 | `max_tokens` 硬编码 + 截断即 failed | **高** | 环境变量 + max_tokens 重试逻辑 | **S4 前必修** |

**推荐实施顺序**（S4 开始前）：问题 4（最痛，直接影响 run 成功率）→ 问题 3（一次性修好永久解决）→ 问题 2（CI 层面先 skip，真修看需求）→ 问题 1（不修，接受现状）。

---

## 附：本次复盘使用的证据

- `runs/20260925-140449-9751ad/` — 15 步失败 run（暴露问题 4 的 max_tokens 截断）
- `runs/20260925-144950-d85a79/` — 45 步成功 run（暴露问题 1 的 DAG 抹平 + 问题 3 的测试污染）
- `runs/20260925-144950-d85a79/.tasks/task_1.json` 至 `task_4.json` — DAG 被抹平后的终态
- `tests/conftest.py:running_daemon` — 未隔离 runs_dir 的 fixture
- `tests/integration/test_s2_dual_process.py:33,79,118` — 产生 "hello"/"broadcast test"/"replay test" 的测试
- `tests/integration/test_s3_task_graph.py` — 5 个 wire 协议测试
- `src/leave_claude/core/task/manager.py:79-80,107-120` — `_clear_dependency` 实现
- `src/leave_claude/core/llm/provider.py:63` — `max_tokens: 4096` 硬编码
- `src/leave_claude/core/loop.py:72-77` — `stop_reason` 处理逻辑
