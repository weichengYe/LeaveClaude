# ADR 0007 — S5 Permission / Tool Execution / Human-in-the-loop 安全边界审查

> 阶段复盘：[S5](../note/s5.md)

- **Status**: Proposed / Deferred
- **Stage**: KamaClaude `stage/s5`
- **Scope**: PermissionPolicy、PermissionManager、Tool Invocation、Bash/File Tools、IPC、CLI/TUI、Permission Persistence、Retry、Tracing
- **Decision timing**: 暂不修改代码；待学习完 S7 后，与 S1–S7 各阶段 ADR 汇总后统一修正
- **Date**: 2026-09-27

---

## 1. 背景

S5 相比 S4 的主要增量，是将 Coding Agent 从：

```text
LLM
 ↓
Tool Call
 ↓
Tool Execution
```

推进到：

```text
LLM
 ↓
Tool Call
 ↓
Permission Policy
 ↓
ALLOW / DENY / ASK
        │
        └── ASK → Human Approval
                        ↓
                  Tool Execution
```

从 `stage/s4` 到 `stage/s5`，GitHub 分支比较显示该阶段集中修改 / 新增了以下能力：

```text
core/permissions/
├── policy.py
├── manager.py
├── storage.py
└── errors.py

core/tools/
├── invocation.py
├── base.py
├── bash.py
├── read_file.py
├── write_file.py
└── list_dir.py

core/bus/
├── commands.py
└── events.py

core/transport/socket_server.py
core/app.py
core/loop.py
core/runner.py
cli/commands/chat.py
tui/app.py
```

并新增大量权限、参数校验、重试、集成测试。

因此，S5 的实质不是“加一个权限弹窗”，而是首次引入了：

```text
Execution Authority
Human-in-the-loop
Permission State
Tool Reliability Policy
Interactive Runtime Coordination
```

这使它成为 Coding Agent Harness 的一个关键阶段。

本 ADR 记录 S5 当前设计中值得保留的部分，以及后续 LeaveClaude 应统一修正的架构问题。

---

# 2. 先固定 S5 中 Permission 的语义

S5 中权限系统应与此前讨论的 `State / Memory / Context` 明确区分。

### 2.1 Permission 不是 Memory

例如：

```text
always_allow bash
```

即使它被持久化到磁盘，也不属于 Agent Memory。

它属于：

```text
Persistent Authorization / Policy State
```

即：

> 用户授予了什么执行能力。

而 Memory 表示的是：

> Agent 过去学到了什么值得未来复用的知识或经验。

因此：

```text
Persistence ≠ Memory
```

---

### 2.2 Permission 也不应该直接成为 LLM Context

权限规则和授权状态主要应该由 Harness 在 Tool Execution Boundary 上强制执行，而不是依赖模型看到：

```text
“你不能执行 xxx”
```

然后自觉遵守。

核心原则：

```text
Agent Intent
≠
Execution Authority
```

LLM 可以提出 Tool Call，但 Harness 决定该 Tool Call 是否真正执行。

---

### 2.3 S5 中已经出现多级 Permission State

当前 PermissionManager 内部存在：

```text
_pending
    ↓
request-scoped transient state

_session_always
    ↓
session-scoped state

_persistent_always
    ↓
persistent/global state
```

这说明 Permission 本身也需要明确生命周期和 scope。

后续设计应继续沿着这一方向扩展，而不是把所有“长期允许”压缩成一个 `always_allow`。

---

# 3. S5 当前正确且应保留的设计

以下设计原则本身是合理的，后续统一重构时应保留。

## 3.1 权限检查发生在 Harness，而不是 LLM

S5 在 `invoke_tool()` 真正调用：

```python
tool.invoke(...)
```

之前执行 PermissionManager。

这是正确的 execution boundary：

```text
LLM Tool Intent
      ↓
Parameter Validation
      ↓
Permission Enforcement
      ↓
Actual Side Effect
```

模型不能绕过权限层自行决定执行。

---

## 3.2 Unknown Tool 默认进入 ASK

未知工具不是默认 ALLOW，而是保守进入人工审批。

这符合 fail-conservative 原则：

```text
Unknown capability
       ↓
Do not auto-execute
```

---

## 3.3 Hard deny / outside boundary 优先于 cached allow

当前优先级大致为：

```text
deny_patterns
    >
outside-cwd forced ASK
    >
session cache
    >
persistent cache
    >
allow_patterns
    >
default
```

因此 cached `always_allow bash` 不能直接覆盖当前的 outside-cwd heuristic。

这个 precedence 思想应保留。

---

## 3.4 ASK 使用 Future 异步挂起

当前权限等待链路：

```text
Tool Call
 ↓
PermissionManager
 ↓
create Future
 ↓
permission.requested
 ↓
await Future
```

用户回复后：

```text
permission.respond
 ↓
Future.set_result(...)
 ↓
原 coroutine 恢复
```

这一设计避免 busy waiting，也不会阻塞整个 asyncio event loop，应保留。

---

## 3.5 Permission denial 被视作环境反馈，而不是直接杀死 Run

用户拒绝工具后，系统返回：

```text
ToolResult(
    is_error=True,
    error_type="permission_denied"
)
```

随后 Agent 可以选择：

```text
换一个 Tool
换一种方案
询问用户
结束任务
```

这比直接抛异常终止整个 Run 更符合 Agent runtime 的语义。

---

## 3.6 Permission Timeout 默认 fail closed

用户长时间不回应时：

```text
Timeout
 ↓
DENY
```

而不是：

```text
Timeout
 ↓
ALLOW
```

这个原则应保留。

---

# 4. ADR-1：Permission System 不能被当作 Sandbox

## 4.1 当前实现

S5 对 Bash 的风险控制主要依赖：

```text
deny regex
outside-cwd regex
allow regex
human approval
```

例如 `policy.py` 通过字符串正则识别：

```text
absolute path
~
../
$HOME
$PWD
cd
```

如果命令看起来可能越出 cwd，则强制 ASK。

---

## 4.2 问题

这些规则只能检查：

```text
command string
```

而不能限制：

```text
真实进程能访问什么文件
真实进程能访问什么网络
真实进程能读取什么环境变量
子进程又启动了什么进程
```

例如：

```bash
python script.py
```

命令本身没有：

```text
/
~
../
cd
```

但 `script.py` 完全可以访问：

```text
/home/user/.ssh/
/etc/
/tmp/
network
```

因此：

```text
Regex Risk Detection
≠
Security Isolation
```

---

## 4.3 决策

后续 LeaveClaude 应明确采用：

```text
Sandbox
+
Permission Escalation
```

而不是：

```text
Permission Prompt
=
Security Boundary
```

目标模型：

```text
           Allowed Sandbox
                 │
         ┌───────┴───────┐
         │               │
Workspace Files      Safe Process
         │               │
         └───────┬───────┘
                 │
          normal execution

Boundary crossing
        ↓
Permission ASK
```

---

## 4.4 Priority

```text
P0
```

但暂不在 S5 阶段实现，待 S7 后统一设计。

---

# 5. ADR-2：引入显式 Workspace Root，不能继续把 process cwd 当安全边界

## 5.1 当前实现

S5 多处使用：

```text
current working directory
```

作为默认项目边界。

但当前没有一个明确的：

```text
Project / Workspace Root object
```

被绑定到 Session / Run / Tools。

Bash 使用：

```python
asyncio.create_subprocess_shell(command)
```

并未显式设置 cwd，因此直接继承 daemon 当前工作目录。

---

## 5.2 问题

这会造成：

```text
安全边界
=
daemon 从哪里启动
```

而不是：

```text
安全边界
=
用户当前项目目录
```

如果 core daemon 从不同目录启动，Agent 的 workspace 语义就会变化。

---

## 5.3 决策

后续应把 Workspace / Project Root 变成一等状态：

```text
Project
└── workspace_root

Session
└── project_id / workspace_root

Run
└── inherit workspace_root
```

所有 Tool 使用同一个显式 root：

```text
ReadFile
WriteFile
ListDir
Bash cwd
Git
Future search tools
```

而不是依赖 Python process cwd。

这也与之前 S4 的 State 设计兼容：

```text
workspace_root
=
Project / Session State
```

而不是 Memory。

---

# 6. ADR-3：统一修复 File Tool 的真实路径 containment

## 6.1 当前实现

`read_file.py`、`write_file.py`、`list_dir.py` 的 tool description 都宣称：

```text
Path must be relative to current working directory.
```

但实现基本只检查：

```python
if ".." in Path(path_str).parts:
    raise PermissionError(...)
```

之后直接使用：

```python
Path(path_str)
```

---

## 6.2 问题一：absolute path 没有真正禁止

例如：

```text
/etc/passwd
```

没有 `..`，因此这一检查不能阻止绝对路径。

尤其 `read_file` 和 `list_dir` 当前默认：

```text
ALLOW
```

所以这里不是单纯的 UX 问题，而是真实边界缺口。

---

## 6.3 问题二：symlink escape

即使禁止绝对路径和 `..`：

```text
workspace/link -> /etc
```

随后：

```text
read_file("link/passwd")
```

字符串仍然完全合法，但 resolve 后已经离开 workspace。

---

## 6.4 决策

所有 filesystem tools 使用同一个 resolver，例如语义上：

```python
root = workspace_root.resolve()
target = (root / user_path).resolve()

if not target.is_relative_to(root):
    reject
```

并集中形成：

```text
WorkspaceFS / PathGuard
```

而不是每个 Tool 单独实现不同的路径检查。

---

# 7. ADR-4：Bash 子进程必须使用 Sanitized Environment

## 7.1 当前实现

`BashTool` 调用：

```python
asyncio.create_subprocess_shell(command, ...)
```

没有显式传入：

```python
env=
```

因此子进程默认继承 core daemon 的环境变量。

---

## 7.2 风险

core daemon 本身可能持有：

```text
ANTHROPIC_API_KEY
其他模型 API Key
代理配置
云凭证
开发环境 Secret
```

因此 Agent 只需要运行：

```bash
env
```

就可能读取 daemon credential。

配合网络访问时，还可能发生 secret exfiltration。

---

## 7.3 决策

后续 Bash / subprocess execution 应使用：

```text
sanitized env allowlist
```

例如只暴露：

```text
PATH
HOME (sandboxed)
LANG
TERM
project-specific safe vars
```

而 daemon 自己的模型密钥不得自动传播到 tool subprocess。

原则：

```text
Harness Credentials
≠
Agent Tool Credentials
```

---

# 8. ADR-5：Network Capability 必须成为独立权限边界

## 8.1 当前实现

当前：

```text
curl
wget
pip install
git clone
python requests
```

全部只是普通 Bash 命令。

没有独立：

```text
network allowlist
network denylist
domain boundary
network sandbox
```

---

## 8.2 问题

Coding Agent 的网络能力和 filesystem 能力不是同一种风险：

```text
Filesystem
→ 本地文件破坏 / secret read

Network
→ secret exfiltration / remote prompt injection / malicious dependency
```

如果全部压缩成：

```text
bash = allow
```

权限粒度过粗。

---

## 8.3 决策

后续将 capability 至少区分为：

```text
Filesystem Read
Filesystem Write
Process Execution
Network Access
External Service / MCP
```

Permission 应围绕 capability，而不是单纯围绕 tool name。

---

# 9. ADR-6：Permission Scope 必须显式化

## 9.1 当前实现

UI 提供：

```text
Allow once
Always allow
Deny
Always deny
```

但 `_apply_response("always_allow")` 会同时写：

```text
_session_always[(session_id, tool_name)]
+
_persistent_always[tool_name]
```

而 persistent policy 位于：

```text
~/.kama/policy.toml
```

因此当前所谓：

```text
Always allow
```

实际接近：

```text
all future sessions / all projects
```

---

## 9.2 一个明显的语义矛盾

代码中存在：

```text
session always cache
```

但 UI 并不存在：

```text
Allow for this session
```

因为任何 `always_allow` 都同时进入 persistent cache。

因此当前 session cache 并没有真正独立的授权语义。

---

## 9.3 决策

后续统一定义：

```text
ONCE
SESSION
PROJECT
GLOBAL
```

例如 UI：

```text
Allow once
Allow for this session
Allow in this project
Always allow globally

Deny once
Deny for this session
Deny in this project
Always deny globally
```

实际 UI 不一定需要同时展示全部选项，但内部数据模型必须具有这些 scope。

---

# 10. ADR-7：Permission Grant 不能只按 tool_name 缓存

## 10.1 当前实现

当前 cache key：

```text
(session_id, tool_name)
```

或：

```text
tool_name
```

例如：

```text
bash → allow
```

---

## 10.2 问题

用户可能只是因为反复执行：

```bash
pytest
```

而点击 Always Allow。

但当前授权可能扩大为：

```bash
rm ...
git reset --hard
python script.py
pip install ...
```

只要不命中 hard deny / outside-cwd heuristic，就可能自动执行。

因此：

```text
Granted capability
>
Original user intent
```

---

## 10.3 决策

长期应演进为：

```text
PermissionGrant
├── effect
├── scope
├── capability / tool
├── operation pattern
├── resource scope
└── origin / created_at
```

例如：

```text
effect = allow
scope = project
capability = process.execute
command_pattern = "pytest *"
resource_scope = workspace
```

或：

```text
effect = allow
scope = project
capability = filesystem.write
path_pattern = "src/**"
```

---

# 11. ADR-8：Static Policy 只能有一个 Source of Truth

## 11.1 当前实现

`policy.py` 已实现：

```text
deny
outside-cwd
allow
default
```

并提供：

```python
evaluate(...)
```

但 `PermissionManager.check_and_wait()` 又重新实现了一次：

```text
deny
outside-cwd
session cache
persistent cache
allow
default
```

---

## 11.2 风险

以后新增规则时很容易出现：

```text
policy.evaluate()
→ DENY

check_and_wait()
→ ALLOW
```

即：

```text
policy drift
```

---

## 11.3 决策

后续采用单一 decision engine：

```text
AuthorizationEngine
        ↓
PolicyDecision
        ↓
PermissionManager
        ↓
Human Approval / Cache
```

具体 precedence 只能存在一份 authoritative implementation。

---

# 12. ADR-9：Permission Decision 使用类型模型，而不是裸字符串

## 12.1 当前实现

IPC command 中：

```python
decision: str
```

允许：

```text
allow_once
always_allow
deny_once
always_deny
```

但协议层没有 Literal / Enum 强校验。

`_apply_response()` 对未知字符串基本会解释成：

```text
allow = False
```

---

## 12.2 决策

后续不要继续扩展字符串组合：

```text
allow_project
always_allow
allow_session
...
```

而应拆成结构化字段：

```text
effect = ALLOW | DENY
scope  = ONCE | SESSION | PROJECT | GLOBAL
```

审批命令、事件、持久化模型统一使用 typed model。

---

# 13. ADR-10：Permission Request 必须绑定真正的 Owner

这是重新检查 IPC 后发现的一个重要问题。

## 13.1 当前实现

`PermissionManager._pending`：

```text
tool_use_id
    ↓
PendingRequest
```

PermissionRespondCommand 只发送：

```text
tool_use_id
decision
```

没有：

```text
session_id
run_id
request_id
client identity
```

同时 TUI / CLI 当前订阅：

```text
scope = global
```

Broadcaster 的 global scope 会接收所有匹配事件。

TUI 对 `permission.requested` 也没有先检查：

```text
event.session_id == current session_id
```

---

## 13.2 风险

如果 daemon 同时存在多个客户端 / Session：

```text
Client A / Session A
Client B / Session B
```

则 A 理论上可能看到 B 的 permission request，甚至使用 tool_use_id 回复。

即使当前 daemon 默认只监听 loopback，这仍然是 IPC ownership 设计上的缺口。

---

## 13.3 决策

后续 PermissionRequest 应拥有独立 identity：

```text
permission_request_id
session_id
run_id
tool_use_id
owner_client_id / connection_id
```

响应必须校验：

```text
response owner
==
request owner
```

同时 event subscription 应支持：

```text
session:<session_id>
```

而不是 chat client 默认全局订阅所有 Session 事件。

---

# 14. ADR-11：Disconnect Cancellation 当前只实现了局部能力，没有真正接上线

## 14.1 当前实现

`PermissionManager` 已实现：

```python
cancel_session(session_id)
```

其目的明确是：

```text
客户端断连
 ↓
将该 Session 的 pending Future resolve 为 deny_once
 ↓
避免僵尸 Run
```

这是正确设计。

---

## 14.2 但生产链路没有连接

`SocketServer._handle_connection()` 在断连时主要执行：

```text
broadcaster.unsubscribe(writer)
writer.close()
```

并没有调用：

```text
PermissionManager.cancel_session(...)
```

而 SocketServer 本身甚至不知道这个连接对应哪个 Session。

因此当前：

```text
cancel_session()
```

更多只存在于 unit test 语义中。

如果：

```text
permission.timeout_s = 0
```

并且客户端断线，pending approval 可能无限等待。

---

## 14.3 决策

需要建立：

```text
Connection
 ↔ Session(s)
 ↔ Pending Permission(s)
```

的 ownership 映射。

断连时：

```text
cancel / deny pending approvals
cancel appropriate UI-owned RPCs
决定 run 是继续、暂停还是取消
```

而不是单纯关闭 socket。

---

# 15. ADR-12：SocketServer 的 handler task 需要显式生命周期管理

## 15.1 当前实现

为了让：

```text
session.send_message
```

这种长 RPC 不阻塞同一连接上的：

```text
permission.respond
```

S5 把每一行请求：

```python
asyncio.create_task(self._handle_line(...))
```

独立执行。

这个方向本身是对的。

---

## 15.2 问题

这些 task 当前：

```text
没有保存引用
没有统一 cancel
没有 connection-level task group
```

客户端断线以后，handler task 可能继续存在。

尤其当 handler 正在：

```text
await Agent Run
await Permission
```

时，生命周期会变得模糊。

---

## 15.3 决策

每个连接应拥有：

```text
ConnectionContext
└── handler_tasks
```

或者使用 structured concurrency / TaskGroup。

连接关闭时应明确：

```text
哪些 command task cancel
哪些 run 独立存活
哪些 permission fail closed
```

---

# 16. ADR-13：`session.send_message` 的 Long-lived RPC 需要重新审视

## 16.1 当前模型

S5：

```text
session.send_message
 ↓
SessionManager.send_message
 ↓
AgentRunner.run_and_capture
 ↓
整个 Run 执行完
 ↓
RPC 才返回 run_id
```

也就是说它实际上是：

```text
completion-style long-lived RPC
```

而不是简单“提交消息”。

---

## 16.2 TUI 已暴露这个问题

TUI 如果直接：

```python
await session.send_message
```

会长期占用 Textual handler。

因此 S5 专门改成：

```text
UI handler
 ↓
run_worker(_do_send_message)
 ↓
handler 立即返回
```

这是一项合理的局部修复，应保留其思想。

---

## 16.3 CLI 仍然存在同类问题

`cli/commands/chat.py` 当前主循环直接：

```python
await client.send_command(
    "session.send_message",
    ...
)
```

而 permission 用户输入也依赖这个同一个主循环继续执行 `_readline()`。

因此可能形成：

```text
CLI 主循环
  ↓ await session.send_message
Agent Run
  ↓ await permission
CLI 输入
  ↓ 只有 send_message 返回后才能继续读取
```

即：

```text
CLI waits Run
Run waits CLI approval
```

当前 Permission unit/integration tests 没有通过真实 CLI + Socket 链路覆盖这一问题。

---

## 16.4 决策

短期：

```text
CLI / TUI 都必须把长 Run 与交互输入路径解耦
```

长期更推荐协议改成：

```text
session.send_message
 ↓
create run
 ↓
return run_id immediately
```

然后：

```text
run.started
llm.token
tool.*
permission.*
run.finished
```

全部通过 event stream 观察。

即：

```text
Command Path
≠
Long-running Execution Path
```

这与 S2 `agent.run → immediate run_id + events` 的模式更一致。

---

# 17. ADR-14：Tool Retry 必须具有 Idempotency Awareness

这是 S5 新增 `tool retry` 后最需要记录的问题之一。

## 17.1 当前实现

`invoke_tool()` 当前会自动重试：

```text
runtime_error
rate_limited
```

最多执行：

```text
1 initial + 2 retries = 3 attempts
```

测试明确验证 runtime_error 会自动重试。

---

## 17.2 问题一：`runtime_error` 太宽

对 Bash 来说：

```text
non-zero exit code
```

也被映射成：

```text
runtime_error
```

例如：

```bash
ls does-not-exist
```

属于确定性失败，却可能被无意义执行 3 次。

---

## 17.3 问题二：Side Effect 可能被重复

更严重的例子：

```bash
step1_with_side_effect && step2_that_fails
```

第一次：

```text
step1 已产生副作用
step2 失败
→ runtime_error
```

runtime 自动 retry 后：

```text
step1 再执行一次
```

因此：

```text
Automatic Retry
+
Non-idempotent Tool
=
Potential Duplicate Side Effects
```

---

## 17.4 问题三：`Allow once` 实际可能执行多次

Permission check 位于 retry loop 之前：

```text
Permission ALLOW ONCE
 ↓
retry loop
 ├── attempt 1
 ├── attempt 2
 └── attempt 3
```

因此用户界面的：

```text
Allow once
```

可能实际上授权同一个 logical Tool Call 执行三次底层 side effect。

这个语义必须明确。

---

## 17.5 决策

Tool 应声明：

```text
retry_safe
idempotent
side_effect_class
```

例如：

```text
read_file
→ retry_safe

list_dir
→ retry_safe

bash
→ default NOT retry_safe

write_file
→ carefully classified
```

同时错误类型应更明确区分：

```text
transient_error
rate_limited
permanent_error
validation_error
permission_denied
```

不要使用一个宽泛 `runtime_error` 驱动统一重试。

---

# 18. ADR-15：Tool Timeout / Cancellation 必须保证 Resource Cleanup

## 18.1 当前结构

目前存在两层 timeout：

```text
invoke_tool outer timeout
+
BashTool internal timeout
```

`invoke_tool` 使用：

```python
asyncio.wait_for(tool.invoke(...), timeout=...)
```

BashTool 内部又对：

```python
proc.communicate()
```

使用 timeout，并在自己的 timeout 路径中 kill subprocess。

---

## 18.2 风险

如果 outer timeout / cancellation 先发生：

```text
BashTool coroutine 被取消
```

内部 cleanup path 未必执行：

```text
proc.kill()
```

从而留下 orphan subprocess。

---

## 18.3 决策

Tool contract 必须定义：

```text
Cancellation-safe
```

对 process tool：

```text
CancelledError / timeout
 ↓
terminate process tree
 ↓
await cleanup
 ↓
return / propagate
```

Long-running external tool 同理。

---

# 19. ADR-16：Policy Persistence 需要 Atomic + Typed + Versioned

## 19.1 当前实现

`storage.py`：

- 自己按行解析 TOML 的 `[always]`
- `save_policy_file()` 使用 `write_text()` 覆盖文件
- 没有 temp file + replace
- 没有 schema version

---

## 19.2 问题

可能出现：

```text
写入过程中 crash
→ policy.toml 部分损坏
```

而未来一旦增加：

```text
project scope
command patterns
resource scopes
policy version
```

手写 parser 会快速复杂化。

---

## 19.3 决策

长期使用：

```text
Typed Policy Model
+
standard TOML parser/writer
+
atomic temp write + os.replace
+
schema version
```

并明确：

```text
Global Policy
Project Policy
Session Grant
```

的存储位置和 ownership。

---

# 20. ADR-17：Trace / Event 中必须考虑 Sensitive Data Redaction

这是检查 S5 observability 链路后新增的 ADR。

## 20.1 当前事件包含大量原始内容

例如：

```text
ToolCallStartedEvent.params
ToolCallFinishedEvent.output
PermissionRequestedEvent.params
```

CoreApp 的 event trace handler 会：

```text
event.model_dump()
 ↓
TraceRecord
```

写入 trace。

因此：

```text
write_file content
bash command
read_file output
tool params
```

可能直接进入日志 / trace 文件。

`TraceConfig.include_llm_payload` 主要控制 LLM tracing，并不能自动解决所有 Tool/Event payload。

---

## 20.2 风险

可能记录：

```text
secret
API key
.env content
credentials
proprietary source
large tool output
```

Observability 不能无条件等于 full payload persistence。

---

## 20.3 决策

统一定义：

```text
Trace Redaction Policy
```

至少支持：

```text
metadata-only
preview-only
redacted payload
full payload (explicit opt-in)
```

Tool schema 可标记：

```text
sensitive fields
```

如：

```text
write_file.content
bash env
HTTP auth headers
future MCP credentials
```

原则：

```text
Useful observability
≠
Persist every raw payload
```

---

# 21. ADR-18：测试层需要覆盖真实 IPC / UI / Lifecycle，而不只是 in-process PermissionManager

## 21.1 S5 当前测试优点

S5 已经新增大量：

```text
permission policy tests
permission manager tests
tool params tests
tool retry tests
integration permission flow tests
```

整体测试意识明显提高，这是应保留的进步。

---

## 21.2 当前 integration test 的局限

`test_s5_permission_flow.py` 使用：

```text
AgentRunner in-process
mock LLM
real PermissionManager
EventBus collector
```

并在收到 `permission.requested` 后直接：

```python
manager.respond(...)
```

因此它验证了：

```text
PermissionManager ↔ AgentRunner
```

但没有验证：

```text
SocketServer
SocketClient
JSON-RPC
TUI / CLI interaction
connection disconnect
multi-client ownership
long-lived RPC liveness
```

所以 CLI 长 RPC + permission input 这类问题不会被发现。

---

## 21.3 决策

后续至少增加以下 E2E 场景：

```text
真实 SocketServer + SocketClient permission flow

CLI chat:
Run 请求权限 → 用户 respond → Run 继续

TUI:
worker + PermissionSelect + respond

client disconnect while permission pending

permission timeout = 0 + disconnect

multiple sessions / multiple clients

absolute path / symlink escape

retry + side-effecting tool

outer timeout + subprocess cleanup
```

---

# 22. S5 对 State / Memory / Context 总架构的影响

S5 进一步证明：

```text
长期保存的信息
```

并不都属于 Memory。

后续 LeaveClaude 的项目数据模型应该至少区分：

```text
Project
│
├── Instructions
├── Memory
├── Security / Permission Policy
├── Workspace State
│
└── Sessions
    ├── Conversation
    ├── Planning State
    ├── Permission Session Grants
    └── Runs
        ├── Tool Execution State
        ├── Pending Permission Requests
        └── Events / Trace
```

其中：

```text
Memory
= historical reusable knowledge

Planning State
= current goal / plan progress

Permission State
= execution authority

Context
= current LLM request projection
```

四者不应混合。

---

# 23. 建议的未来 Permission Architecture

统一修正后的目标结构可考虑：

```text
                   Tool Intent
                      │
                      ▼
              Parameter Validation
                      │
                      ▼
               Capability Model
                      │
          ┌───────────┼────────────┐
          │           │            │
          ▼           ▼            ▼
      Sandbox      Policy      Cached Grant
          │           │            │
          └───────────┼────────────┘
                      │
                Authorization
                      │
          ┌───────────┼───────────┐
          │           │           │
        ALLOW        DENY         ASK
                                  │
                                  ▼
                           Human Approval
                                  │
                                  ▼
                           Typed Grant
                                  │
                                  ▼
                         Tool Invocation
                                  │
                                  ▼
                    Idempotency-aware Retry
                                  │
                                  ▼
                      Cancellation-safe Cleanup
```

---

# 24. Priority 汇总

| Priority | ADR | 说明 |
|---|---|---|
| **P0** | Permission ≠ Sandbox | 权限弹窗不能成为唯一安全边界 |
| **P0** | Explicit Workspace Root | 不能依赖 daemon process cwd |
| **P0** | Real path containment | 修复 absolute path / symlink escape |
| **P0** | Sanitized subprocess env | 不向 Tool 泄露 daemon secrets |
| **P0** | Network capability boundary | 网络能力单独建模 |
| **P0** | Retry idempotency | 防重复副作用，明确 Allow once 语义 |
| **P1** | Permission scope | once/session/project/global |
| **P1** | Capability-scoped grants | 不只按 tool_name 授权 |
| **P1** | Request ownership | session/run/client 绑定审批 |
| **P1** | Disconnect cancellation wiring | 真正接通 cancel_session |
| **P1** | Long-lived RPC redesign | submit-and-observe |
| **P1** | Cancellation-safe tool runtime | 防 orphan subprocess |
| **P1** | Single policy engine | 防规则漂移 |
| **P1** | Typed decisions | IPC / storage 统一类型化 |
| **P1** | Sensitive trace redaction | 防 secret 落盘 |
| **P2** | Atomic/versioned policy store | 提升持久化可靠性 |
| **P2** | Connection task lifecycle | structured concurrency |
| **P2** | End-to-end IPC/UI tests | 补齐真实交互覆盖 |

---

# 25. 当前阶段暂不实施的内容

根据当前学习策略，S5 阶段：

```text
不立即实现 sandbox
不立即重写 permission model
不立即修改 task / memory / context
不立即重构 IPC
```

先保留 ADR。

继续学习 S6 / S7 后再统一判断：

```text
哪些问题已在后续 stage 被修复
哪些问题持续存在到 S7
哪些修改应该进入 LeaveClaude 最终架构
```

这样避免：

```text
在 S5 修一个问题
S6 / S7 又重新设计一次
```

---

# 26. 最终决策摘要

S5 的核心架构价值应保留：

```text
Tool Call
→ Harness Permission Gate
→ Human-in-the-loop
→ Tool Result
```

但最终 LeaveClaude 不应停留在：

```text
regex + tool-name allow cache + approval popup
```

而应演进到：

```text
Sandboxed Execution
+
Explicit Capability Model
+
Scoped Authorization State
+
Human Escalation
+
Idempotency-aware Tool Runtime
+
Lifecycle-safe Async Execution
+
Redacted Observability
```

S5 最重要的学习结论可以压缩成四句话：

```text
1. Agent Intent ≠ Execution Authority
2. Permission ≠ Sandbox
3. Persistence ≠ Memory
4. Tool Retry ≠ Safe Retry
```

以及一个贯穿 S4–S5 的统一原则：

```text
所有长期数据都必须明确：
它是什么状态？
属于哪个 scope？
谁拥有它？
什么时候失效？
是否应该进入 LLM Context？
```

---

## 27. S5 源码审查范围

本 ADR 基于 KamaClaude `stage/s4 → stage/s5` 的 GitHub 增量审查，重点检查：

```text
src/kama_claude/core/permissions/policy.py
src/kama_claude/core/permissions/manager.py
src/kama_claude/core/permissions/storage.py
src/kama_claude/core/tools/invocation.py
src/kama_claude/core/tools/base.py
src/kama_claude/core/tools/builtin/bash.py
src/kama_claude/core/tools/builtin/read_file.py
src/kama_claude/core/tools/builtin/write_file.py
src/kama_claude/core/tools/builtin/list_dir.py
src/kama_claude/core/bus/commands.py
src/kama_claude/core/bus/events.py
src/kama_claude/core/transport/socket_server.py
src/kama_claude/core/transport/ipc_broadcaster.py
src/kama_claude/core/app.py
src/kama_claude/core/loop.py
src/kama_claude/core/runner.py
src/kama_claude/cli/commands/chat.py
src/kama_claude/tui/app.py
src/kama_claude/core/config.py

tests/unit/test_permission_manager.py
tests/unit/test_permission_policy.py
tests/unit/test_tool_params.py
tests/unit/test_tool_retry.py
tests/integration/test_s5_permission_flow.py
```

该 ADR 记录的是 **S5 源码事实 + 后续 LeaveClaude 架构决策候选**，不代表当前阶段立即改动代码。
