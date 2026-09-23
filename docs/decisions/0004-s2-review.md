# ADR 0004: S2 传输层与运行时健壮性审查(待实施)

## 状态
已审查,**待实施**(计划在学完 S7 后统一修改,届时对照本 ADR 执行)

## 背景
S2 同步完成后,对 `core/transport/`、`tui/app.py`、`cli/commands/core.py`、`core/app.py` 做了逐行 review,共发现 10 处可改进点(2026-09-23 复审追加第 11 处,见改动 1)。其中 8 处确认 KamaClaude 直到 S7 分支也未修复(逐条对照过 S7 源码)。本 ADR 记录**优先修的 3 个**,每个都包含:现状、修法、测试设计——作为后续实施的唯一依据。

**为什么现在不改**:每次从 KamaClaude 同步新阶段都会整树覆盖,现在改会被 S3–S7 的同步反复冲掉。等 S7 学完(最后一次同步),再按本 ADR 一次性修改并补测试,之后不再有同步冲突。

**为什么是这 3 个**:它们共同解决"慢客户端不能拖死 Core、坏 RPC 不能挂死 Client、坏 Run 不能变成没人管的后台 Task"——即运行时健壮性。其余 7 个(TUI elif 链、PID 竞态、退避重连等)不影响正确性,见文末附录。

---

## 改动 1: Broadcaster drain timeout + 按连接去重(慢客户端隔离)

### 现状
`core/transport/ipc_broadcaster.py` 的 `handle()`:

```python
for sub in list(self._subscriptions):
    ...
    sub.writer.write(envelope.model_dump_json().encode() + b"\n")
    await sub.writer.drain()   # ← 无超时
```

一个慢订阅者(TUI 挂起/网络卡)会让 `drain()` 无限等待,**阻塞整条事件链路**:Broadcaster → EventBus → AgentLoop。S7 的代码同样如此,且循环里还加了 trace emit,慢客户端的代价更大。

复审追加发现的两个结构性问题:

**(a) 同一连接重复推送(正确性 bug)**。数据模型是扁平的 `list[_Subscription]`,每个订阅独立持有 writer。同一客户端建立多个 topic 重叠的订阅(如 `sub1: ["tool.*"]` + `sub2: ["*"]`)时,一个 `tool.call_started` 事件会**命中两个订阅、向同一 writer 写两次**——客户端收到重复事件。

**(b) 隔离边界错位**。决定发送速度/backpressure 的是 TCP 连接(writer),而 subscription 只是过滤规则。扁平订阅列表把两者混为一谈:同一连接下的多个订阅天然共享网络状况,一个连接慢,它名下所有订阅都慢;更要紧的是,当前实现逐订阅串行 `drain()`,任何连接慢都会拖住其他连接和上游 AgentLoop。

### 修法(两处合一)
1. **drain 加 1 秒超时**,超时视为慢连接,踢出该连接的全部订阅
2. **payload 序列化移出循环**:同一事件广播给 N 个订阅者,`model_dump_json()` 只需一次
3. **按连接去重**:单次 handle 调用内,同一 writer 只写一次(任一订阅命中即算命中)

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

### 目标架构(明确为 per-connection,而非 per-subscription)

长期正确方案是**按连接建立 outbound queue + sender task**,而不是按订阅:

```
Broadcaster(topic/scope 路由)
   ├── Connection A: writer_A, queue_A, sender_A, [sub: tool.*]
   ├── Connection B: writer_B, queue_B, sender_B, [sub: *, sub: run.*]
   └── Connection C: writer_C, queue_C, sender_C, [sub: *]
```

`handle()` 退化为 O(匹配连接数) 次非阻塞 `queue.put_nowait()`,TCP 写入由各连接的 sender task 独立承担——慢连接只堵自己的队列,不碰别人。这同时自然解决重复推送(按连接入队一次)。

Queue 化必须同时回答的问题(实施时逐项决策):
- **maxsize 与队满策略**:`llm.token` / debug log 属可丢的高频事件,`run.finished` / `tool.call_failed` 属必须保留的关键事件——需要分级丢弃(QoS),而不是无界队列吃光内存或一律丢头
- **sender task 生命周期**:连接断开时 cancel,避免任务泄漏
- **drain timeout 是否保留**:queue 化后 sender 内部仍需写超时,作为 final backstop

### 为什么 S2 仍先用 timeout 过渡
S2 的真实部署形态是单用户(1 CLI + 1 TUI,连接数 ≤ 2),drain timeout 已把"最坏拖 1 秒"封顶;而 queue 化引入任务生命周期与 QoS 两块新设计面,放在 S3 协议演进里与事件 `seq` 游标(见附录 Replay→Live 一条)一起做更合理。

### 测试设计(`tests/unit/test_ipc_broadcaster.py` 新增)

**T1.1 慢订阅者不阻塞其他订阅者**
- 布局:两个订阅者,A 的 `writer.drain` mock 成永不返回(如 `asyncio.Event().wait()`);B 正常
- 步骤:`handle(event)` 用 `asyncio.wait_for(..., timeout=3)` 包裹
- 断言:整体在 3s 内完成;B **收到了** payload;A 被加入 dead 并 unsubscribe
- 现状(未修)此测试必然超时失败——这就是"失败测试先行"

**T1.2 慢订阅者被清理后不再收事件**
- 布局:T1.1 触发一次 handle 后,继续 `handle` 第二个事件
- 断言:A 的 writer 不再被写入;订阅列表长度恢复

**T1.3 payload 只序列化一次**
- 方式:monkeypatch `EventPushEnvelope.model_dump_json` 计数
- 断言:2 个订阅者匹配同一事件时,序列化次数 == 1(而非 2)

**T1.4 同一连接 topic 重叠订阅不重复推送**
- 布局:同一 writer 注册两个订阅(`["tool.*"]` 与 `["*"]`),再注册另一连接的正常订阅
- 步骤:派发一个 `tool.call_started`
- 断言:重叠连接的 writer 只收到 **1 次**;另一连接正常收到 1 次
- 现状(未修)此测试失败(收到 2 次)——正确性 bug 的失败测试先行

### T1.5(集成级,可选)
真实 TCP 场景:订阅者读端不读数据、SO_RCVBUF 塞满,验证 1s 内 handle 返回。沙箱跑不了 raw socket,归入本地/CI 执行清单。

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
[ ] T1.1–T1.4 失败测试先行 → 改 handle()(drain timeout + 序列化外提 + 按连接去重)→ 测试转绿
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
| broadcaster `unsubscribe` O(n) | 客户端个位数,量级无感;queue 化(按 Connection 重建数据模型)时一并解决——`unsubscribe`/去重/队列管理统一到 Connection 层 |
| `Popen stderr=DEVNULL` 吞启动错误 | 影响的是排障体验,不影响正确性;可加 `--foreground` 调试模式替代 |
| PID 复用误判(`kill(pid,0)` 无 cmdline 校验) | 概率极低;修法(`cat /proc/<pid>/cmdline` 校验)平台相关,不值得引入 |
| TUI 重连固定 2s 无退避 | 单用户 TUI,刷屏问题大于功能问题 |
| `run_event_loop` 无优雅停止 | cancel task 目前够用;与 queue 化改造(S3)一起做 |
| Replay→Live gap(replay 与 subscribe 之间的事件丢失) | 正确解是事件协议加 `seq` 字段 + client 维护 `after_seq` 游标,属 S3 协议演进;S2 先明确 replay 是 best-effort |
