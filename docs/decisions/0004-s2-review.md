# ADR 0004: S2 传输层与运行时健壮性审查(待实施)

## 状态
已审查,**待实施**(计划在学完 S7 后统一修改,届时对照本 ADR 执行)

## 背景
S2 同步完成后,对 `core/transport/`、`tui/app.py`、`cli/commands/core.py`、`core/app.py` 做了逐行 review,共发现 10 处可改进点(2026-09-23 复审追加第 11 处,见改动 1)。其中 8 处确认 KamaClaude 直到 S7 分支也未修复(逐条对照过 S7 源码)。本 ADR 记录**优先修的 3 个**,每个都包含:现状、修法、测试设计——作为后续实施的唯一依据。

**为什么现在不改**:每次从 KamaClaude 同步新阶段都会整树覆盖,现在改会被 S3–S7 的同步反复冲掉。等 S7 学完(最后一次同步),再按本 ADR 一次性修改并补测试,之后不再有同步冲突。

**为什么是这 3 个**:它们共同解决"慢客户端不能拖死 Core、坏 RPC 不能挂死 Client、坏 Run 不能变成没人管的后台 Task"——即运行时健壮性。其余 7 个(TUI elif 链、PID 竞态、退避重连等)不影响正确性,见文末附录。

---

## 改动 1: Broadcaster drain timeout + 按连接去重(本地形态下收敛)

### 范围限定(本次复审的修正)
LeaveClaude 当前是**本地 coding agent 框架**:Core daemon 监听 `127.0.0.1`(见 `core/config.py:11` 的 `_DEFAULT_HOST = "127.0.0.1"` 与 `RUNBOOK.md:58` 的 `LEAVE_HOST` 默认值),客户端是单用户机器上的 CLI 与 TUI。典型连接数 ≤ 2(1 CLI + 1 TUI),不存在"一个 server 服务多个相互独立的客户端"的场景。

这一架构事实把原 ADR 中部分设计面的紧迫性降下来了:

- **(b) 隔离边界错位 + per-connection queue/sender task 长期方案**:这是面向"多客户端互不拖累"的方案,本地形态下没有触发动机——同一时刻最多一个 CLI + 一个 TUI,且 TUI 慢就让 TUI 自己掉线重连即可,不存在"别人被拖累"的受害者。**本 ADR 不实施 queue 化,标记为 deferred**,仅在 LeaveClaude 演进为 cloud agent(单个 server 后端服务多个独立 agent session / 多用户)时再启动。届时本 ADR 第 "目标架构(已 deferred)" 一节是入口。
- **(a) 同一连接重复推送**:仍是正确性 bug,与客户端数量无关,**必修**——本地形态下一个 TUI 也可能建立多个 topic 重叠的订阅(冷启动续订 + 全量订阅同时存在等场景),事件被重复推送会让 TUI 重复渲染。
- **drain 无超时**:仍是健壮性问题,本地形态下也会触发(TUI 挂起/内核缓冲满),**必做**。
- **payload 序列化移出循环**:零成本优化,与客户端数量无关,**必做**。

### 现状(本地形态下重述)
`core/transport/ipc_broadcaster.py` 的 `handle()`:

```python
for sub in list(self._subscriptions):
    ...
    sub.writer.write(envelope.model_dump_json().encode() + b"\n")
    await sub.writer.drain()   # ← 无超时
```

两个问题:

1. **drain 无限等**:`asyncio.StreamWriter.drain()` 在 TCP send buffer 满时会让出直到对端读取。本地形态下若 TUI 的读循环卡住(渲染 hung、终端焦点切走、pdb 断点等),`drain()` 长时间不返回,AgentLoop 通过 EventBus → Broadcaster 的整条链路被堵。
2. **同连接重复推送**:扁平 `list[_Subscription]` 让多个 topic 重叠的订阅各自调用 `writer.write()`——同一 writer 在一次 `handle()` 内被写多次,客户端收到重复事件。

### 修法(三处合一,但范围限定为本地形态)
1. **drain 加 1 秒超时**,超时视为慢/死连接,踢出该 writer 名下的全部订阅并 `close()`——这一动作同时兼任"对端已断开"的清理路径(本地 TUI 异常退出后 socket 半开,丢数据会触发 BrokenPipe)。
2. **payload 序列化移出循环**:同一事件广播给多个订阅者,`model_dump_json()` 只需一次。
3. **按连接去重**(单次 `handle()` 内):同一 writer 只写一次,任一订阅命中即算命中——修 (a) 的正确性 bug。

```python
_DRAIN_TIMEOUT_S = 1.0

async def handle(self, event: BaseModel) -> None:
    event_dict = event.model_dump()
    event_type: str = event_dict.get("type", "")
    run_id: str | None = event_dict.get("run_id")

    payload = EventPushEnvelope(event=event_dict).model_dump_json().encode() + b"\n"

    dead: list[asyncio.StreamWriter] = []
    seen: list[asyncio.StreamWriter] = []
    for sub in list(self._subscriptions):
        if sub.writer in seen:
            continue
        if not self._matches_topic(event_type, sub.topics):
            continue
        if not self._matches_scope(run_id, sub.scope):
            continue
        try:
            sub.writer.write(payload)
            await asyncio.wait_for(sub.writer.drain(), timeout=_DRAIN_TIMEOUT_S)
            seen.append(sub.writer)
        except (TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
            logger.warning("dropping slow/dead subscriber %s", sub.sub_id)
            dead.append(sub.writer)

    for writer in dead:
        self.unsubscribe(writer)   # 移除该 writer 名下全部订阅
        writer.close()
```

说明:`dead` 列表在本地形态下基本只命中 `TimeoutError`(对端读循环卡住)与 `BrokenPipeError`(对端进程已退);`unsubscribe(writer)` 顺手清掉该 writer 残留的旧订阅条目,免得重连后重复计数。

### 目标架构(已 deferred,仅 cloud agent 时启动)
长期正确方案是**按连接建立 outbound queue + sender task**——但**这不在本 ADR 的实施范围**。

```
Broadcaster(topic/scope 路由)
   ├── Connection A: writer_A, queue_A, sender_A, [sub: tool.*]
   ├── Connection B: writer_B, queue_B, sender_B, [sub: *, sub: run.*]
   └── Connection C: writer_C, queue_C, sender_C, [sub: *]
```

触发条件(任一即可启动 queue 化):
- LeaveClaude 引入 **cloud agent 模式**:单个 server 后端服务多个用户 / 多个独立 agent session,需要"一个客户端慢不能拖累其他客户端"
- 事件频率显著上升(例如加入 `llm.token` 流式事件、debug log 推送),逐订阅同步 `drain()` 在本地也开始卡顿
- 多播 / pub-sub 外部消费者接入(例如 metrics collector、审计服务)

届时需要同时回答的设计问题(预先列出,实施时逐项决策):
- **maxsize 与队满策略(QoS)**:`llm.token` / debug log 属可丢的高频事件,`run.finished` / `tool.call_failed` 属必须保留的关键事件——需要分级丢弃,而不是无界队列吃光内存或一律丢头
- **sender task 生命周期**:连接断开时 cancel,避免任务泄漏
- **drain timeout 保留**:queue 化后 sender 内部仍需写超时,作为 final backstop
- **数据模型迁移**:扁平 `list[_Subscription]` → `dict[StreamWriter, Connection]`,`unsubscribe`/去重/队列管理统一到 Connection 层(同步解掉附录中 "broadcaster `unsubscribe` O(n)" 一项)

### 测试设计(`tests/unit/test_ipc_broadcaster.py` 新增)

**T1.1 慢订阅者不阻塞 handle**(本地形态下的关键不变量)
- 布局:单个订阅者,`writer.drain` mock 成永不返回(如 `asyncio.Event().wait()`)
- 步骤:`handle(event)` 用 `asyncio.wait_for(..., timeout=3)` 包裹
- 断言:整体在 ~1s 内完成;该订阅者被加入 dead 并 `unsubscribe`;`writer.close()` 被调用

**T1.2 慢订阅者被清理后不再收事件**
- 布局:T1.1 触发一次 `handle` 后,继续 `handle` 第二个事件
- 断言:原订阅者 writer 不再被写入;订阅列表恢复

**T1.3 payload 只序列化一次**
- 方式:monkeypatch `EventPushEnvelope.model_dump_json` 计数
- 断言:多个订阅者匹配同一事件时,序列化次数 == 1(而非 N)

**T1.4 同一连接 topic 重叠订阅不重复推送**(正确性 bug 的失败测试先行)
- 布局:同一 writer 注册两个订阅(`["tool.*"]` 与 `["*"]`)
- 步骤:派发一个 `tool.call_started`
- 断言:该 writer 只收到 **1 次**;`writer.write` 调用次数 == 1
- 现状(未修)此测试必然失败——这就是"失败测试先行"

**T1.5(可选,本地形态下收益小)** 真实 TCP 半开连接:订阅者读端不读数据、SO_RCVBUF 塞满,验证 ~1s 内 `handle` 返回。沙箱跑不了 raw socket,归入本地/CI 执行清单。**注:此条在本地形态下重要性大幅下降,因为只有 TUI 自己慢这一种触发路径;真正需要它的是 cloud agent 形态下的多客户端互扰。**

---

## 改动 2: SocketClient RPC 生命周期(timeout + pending 清理)

### 现状
`core/transport/socket_client.py`:

```python
async def send_command(self, method, params) -> dict[str, Any]:
    ...
    self._pending[req_id] = fut
    self._writer.write(...)
    await self._writer.drain()
    return await fut          # ← 无超时,永久挂起
```

两个问题:
1. daemon 收到请求但 handler 不返回 → `await fut` 永挂,TUI 卡死
2. 永挂的 future 永远留在 `_pending` dict → 内存泄漏

另外 `_dispatch()` 对"未知/已过期 id 的迟到响应"**静默丢弃**,排障时无法区分"server 没回"和"回了但被丢"。

### 修法
```python
async def send_command(
    self, method: str, params: dict[str, Any], timeout: float = 5.0
) -> dict[str, Any]:
    if self._writer is None:
        raise RuntimeError("not connected — call connect() first")
    req_id = str(uuid.uuid4())
    request = JsonRpcRequest(id=req_id, method=method, params=params)
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    self._pending[req_id] = fut
    try:
        self._writer.write(request.model_dump_json().encode() + b"\n")
        await self._writer.drain()
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError as exc:
        raise IpcError(-1, f"RPC timeout: {method}") from exc
    finally:
        self._pending.pop(req_id, None)   # ← 关键:超时后清理
```

`_dispatch()` 补:
```python
if req_id and req_id in self._pending:
    ...
else:
    logger.warning("received response for unknown/expired request id %s", req_id)
```

**timeout 默认 5s 的理由**:`agent.run` 是"创建后台 task 立即返回 run_id"的模式(见 `core/app.py:_agent_run_handler`),5–10s 已非常宽裕;真正长耗时通过 `run.*` 事件流体现,不需要长 timeout 的 RPC。

### 测试设计(`tests/unit/test_socket_client.py` 新增)

**T2.1 server 无响应 → timeout 抛 IpcError**
- 布局:fake server 收请求后**不回**
- 断言:`send_command` 在 ~timeout 后抛 `IpcError`,code == -1,message 含 "timeout"

**T2.2 timeout 后 `_pending` 清理**
- 布局:T2.1 触发后
- 断言:`client._pending` 为空(dict size == 0)

**T2.3 迟到响应不 crash、有 warning**
- 布局:T2.1 的 timeout 抛出后,fake server 再发一个相同 id 的响应
- 步骤:驱动 `run_event_loop` 处理这行输入(用 caplog 捕获)
- 断言:不抛异常;caplog 有 "unknown/expired request id" WARNING

**T2.4 正常路径不受影响**
- 布局:fake server 收到请求立即回标准 response
- 断言:`send_command` 返回 result dict;`_pending` 清空

**T2.5(回归)现有 4 个 socket_client 测试保持通过**

---

## 改动 3: Agent Run Task 监督(supervision)与优雅关停

### 现状
`core/app.py`:

```python
self._current_run_task = asyncio.create_task(
    runner.run(cmd.goal, run_id=run_id)
)
return AgentRunResult(run_id=run_id)
```

创建后**无人监管**。两个后果:
1. `runner.run()` 抛未捕获异常 → "Task exception was never retrieved",run 神秘死亡,daemon 无感知
2. daemon 收到 SIGTERM → `await shutdown.wait()` → `await server.stop()` 直接退出,**正在跑的 Run(Task 还在等 LLM/工具)被硬杀**,没有取消、没有清理

### 修法

**3a. supervision wrapper**

```python
async def _run_agent(self, runner: AgentRunner, goal: str, run_id: str) -> None:
    try:
        await runner.run(goal, run_id=run_id)
    except asyncio.CancelledError:
        logger.info("run %s cancelled", run_id)
        raise
    except Exception:
        logger.exception("unhandled exception in run %s", run_id)
```

`_agent_run_handler` 中改为:
```python
self._current_run_task = asyncio.create_task(
    self._run_agent(runner, cmd.goal, run_id),
    name=f"agent-run-{run_id}",       # task 命名,便于日志/调试
)
```

**3b. shutdown 时取消在跑的 Run**

```python
await shutdown.wait()
logger.info("shutting down")

if self._current_run_task is not None and not self._current_run_task.done():
    self._current_run_task.cancel()
    try:
        await asyncio.wait_for(self._current_run_task, timeout=5.0)
    except (asyncio.CancelledError, TimeoutError):
        pass

await server.stop()
```

关停顺序:SIGTERM → 不再接受新任务(现有互斥已有)→ cancel 当前 Run → 等待清理(最多 5s)→ 关 SocketServer → 退出。

### 测试设计(`tests/unit/test_runner.py` 或新建 `tests/unit/test_app_supervision.py`)

**T3.1 runner 抛异常 → daemon 不崩、异常被记录**
- 布局:注入一个 `runner.run` 抛 `RuntimeError("boom")` 的 fake AgentRunner
- 步骤:调 `_agent_run_handler`;等 task 完成
- 断言:handler 正常返回 run_id;task done 且无 "exception was never retrieved";caplog 有 "unhandled exception in run"

**T3.2 shutdown 时 cancel 在跑的 Run**
- 布局:`runner.run` mock 成 `await asyncio.sleep(60)`
- 步骤:起 handler 拿到 run_id → 直接调 `run()` 的 shutdown 分支(把 shutdown event 手动 set,或将关停逻辑抽成 `_shutdown_run_task()` 单测)
- 断言:5s 内 task 结束;task 状态为 cancelled

**T3.3 CancelledError 语义正确**
- 布局:fake runner 在 `CancelledError` 时打点后 re-raise
- 断言:wrapper 不吞掉 CancelledError(task 最终是 cancelled 状态);日志有 "run ... cancelled"

**T3.4 新 Run 互斥不受影响**
- 回归:第一个 run 进行中时再调 `_agent_run_handler` 仍抛 "a run is already in progress"

---

## 实施清单(学完 S7 后执行)

```
[ ] T1.1–T1.4 失败测试先行 → 改 handle()(drain timeout + 序列化外提 + 按连接去重)→ 测试转绿;T1.5 本地形态下收益小,可选
[ ] T2.1–T2.4 失败测试先行 → 改 send_command()/_dispatch() → 测试转绿
[ ] T3.1–T3.4 失败测试先行 → 改 _agent_run_handler()/run() → 测试转绿
[ ] 回归:ruff + mypy strict + pytest 全量
[ ] 更新本 ADR 状态为"已实施",补 commit 号
```

---

## 附录:已审查、确认不修的 7 项(及理由)

| 问题 | 不修理由 |
|---|---|
| TUI `_handle_event` elif 链(15 个分支) | 不引发挂死/泄漏;等事件类型爆炸(S5+ 权限/审批事件)再拆 handler registry |
| broadcaster `unsubscribe` O(n) | 客户端个位数,量级无感;本 ADR 不做 queue 化,此 O(n) 在本地形态下不构成问题;若未来演进出 cloud agent 触发 queue 化,数据模型迁移到 per-Connection 时一并解决(详见改动 1 "目标架构(已 deferred)" 一节) |
| `Popen stderr=DEVNULL` 吞启动错误 | 影响的是排障体验,不影响正确性;可加 `--foreground` 调试模式替代 |
| PID 复用误判(`kill(pid,0)` 无 cmdline 校验) | 概率极低;修法(`cat /proc/<pid>/cmdline` 校验)平台相关,不值得引入 |
| TUI 重连固定 2s 无退避 | 单用户 TUI,刷屏问题大于功能问题 |
| `run_event_loop` 无优雅停止 | cancel task 目前够用;queue 化不在本 ADR 实施范围内(改动 1 已标 deferred),此条留待真正需要时再启动 |
| Replay→Live gap(replay 与 subscribe 之间的事件丢失) | 正确解是事件协议加 `seq` 字段 + client 维护 `after_seq` 游标,属 S3 协议演进;S2 先明确 replay 是 best-effort |
