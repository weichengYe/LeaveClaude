# ADR 0003: S0 阶段的两个跟进改进

## 状态
已采纳 (2026-09-21)

## 背景
S0 完成后,逐行 review `src/leave_claude/core/bus/envelope.py` 和 `src/leave_claude/core/transport/socket_server.py`,发现两处真实问题。两者都是"当时能跑、S0 阶段不可见,但会在 S1+ 阶段翻车"的隐患。本 ADR 记录这两处改动的原因和取舍。

## 决策 1: JSON-RPC `id` 类型从 `str` 放宽到 `str | int`

### 现状
```python
class JsonRpcRequest(BaseModel):
    id: str
```

### 问题
JSON-RPC 2.0 规范(spec §4, [jsonrpc.org/specification](https://www.jsonrpc.org/specification))明确:

> An identifier established by the Client that MUST contain a String, Number, or NULL value if included.

S0 阶段 CLI 自己发请求,id 都是 string,问题不可见。但 S2+ 引入事件订阅后,客户端可能是:
- 其他语言的 SDK(e.g. TypeScript / Go),惯用 number id
- MCP 协议桥(协议层用 number id 更常见)
- 第三方工具生成的客户端

任何发 `"id": 42` 的请求都会被 pydantic 拒掉,返回 `INVALID_REQUEST`,**错误信息不准确**(真实的"协议兼容问题"被报成"格式错误")。

### 取舍
- `str | int` 还是 `str | int | None`?错误响应的 id 可能是 `None`(请求本身无效,没法用 id 反查),所以走 `str | int | None`
- 不支持嵌套类型(对象/数组):JSON-RPC 规范明确禁止,**严格按规范来**

### 验证
新增两个测试 `test_request_int_id_accepted` 和 `test_success_int_id_roundtrip`,覆盖 int id 的 validate 与 serialize 往返。

## 决策 2: `SocketServer.start()` 先 bind 再探测

### 现状
```python
async def start(self) -> str:
    try:
        _r, w = await asyncio.open_connection(self._host, self._port)
        w.close()
        await w.wait_closed()
        raise SystemExit(f"core already running at {self._host}:{self._port}")
    except (ConnectionRefusedError, OSError):
        pass
    self._server = await asyncio.start_server(...)
```

### 问题
两处缺陷:

1. **误判** —— "端口能连上" 不等于 "已有 core 在跑"。可能是另一个服务碰巧占了 7437。这种情况被误报为 `core already running`,误导排障(用户去查 core 进程,但其实根本不是 core)。

2. **TOCTOU** —— 探测与 bind 之间存在窗口。即便探测时端口空着,后续 bind 时其他进程抢先占用,`start_server` 会抛 `OSError`,但用户看到的错误信息仍说 "already running"——因为先前的探测"成功"了。

### 取舍
- **新顺序**:`start_server()` 先尝试 bind,失败时再探测端口。bind 成功 → 正常启动;bind 失败且探测能连上 → "already running";bind 失败且探测也连不上 → 报原始 `OSError`
- **`from exc` 保留 traceback**:用户能看到真正的底层错误(比如 `Permission denied`),不只是包装后的 message
- **不抓所有 `OSError`**:只有 `start_server` 抛出的才算"bind 失败";后续的探测 OSError 不应干扰

### 验证
原行为没测试覆盖(沙箱禁 raw socket,真启动无感)。改后的逻辑以代码形式读起来比测试覆盖更直观;后续在本地或 CI 跑 `tests/integration/test_ping_roundtrip.py` 时,真发生 `bind` 冲突场景会自动验证。

## 影响
- S1+ 的所有新 envelope 都基于放宽后的 `id` 类型,无需再改
- `start()` 的错误信息更准确,排障时间预计减半(用户立刻知道是端口冲突还是别的服务)
- 协议兼容性更接近 JSON-RPC 2.0 spec,接外部 SDK/MCP 时不会有兼容性 wall

## 反向思考(没改但留作未来)
- `free_port` fixture 的 TOCTOU 仍是真实隐患(close 后再 bind 中间窗口)。**最严谨的解**是让 daemon 支持继承 socket fd(`LEAVE_LISTEN_FD`),需要改动较大,留到 S2+ 阶段
- handler 返回类型 `BaseModel | dict` 的鸭子类型问题:dispatcher 用 `isinstance(result, BaseModel)` 判断。**真正解**是统一为 dict 返回,handler 内部 `model_dump()`——等 S1 加更多 handler 时重构
