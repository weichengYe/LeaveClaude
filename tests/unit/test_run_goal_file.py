from __future__ import annotations

import sys
from pathlib import Path

import pytest

from leave_claude.cli import main as main_mod
from leave_claude.cli.commands import run as run_mod
from leave_claude.cli.commands.run import GoalFileError, cmd_run, read_goal_from_file
from leave_claude.core.config import LeaveConfig

# ── read_goal_from_file ──────────────────────────────────────────────────────


# 功能：验证从文件中读取 goal 时会去除首尾空白，保留内部换行
# 设计：文件内容带上前后空行与缩进，断言返回的是 trim 后的字符串，覆盖最常见的真实输入
def test_read_goal_from_file_strips_surrounding_whitespace(tmp_path: Path) -> None:
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("\n  修复登录 bug\n  复现步骤见 issue\n\n", encoding="utf-8")

    assert read_goal_from_file(str(goal_file)) == "修复登录 bug\n  复现步骤见 issue"


# 功能：验证文件不存在时抛出带 "not found" 信息的 GoalFileError
# 设计：用一个确定不存在的路径触发 FileNotFoundError 分支，断言异常类型和消息关键词，
#       确保 CLI 后续能打印面向用户的可读错误而非原始堆栈
def test_read_goal_from_file_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(GoalFileError) as exc:
        read_goal_from_file(str(missing))
    assert "not found" in str(exc.value)


# 功能：验证空文件或仅含空白字符的文件被视为空 goal 并报错
# 设计：仅写空白内容以命中 strip 后为空的判断，断言错误消息含 "empty"，
#       避免把空 goal 发送给 daemon 造成无意义的 run
def test_read_goal_from_file_empty_raises(tmp_path: Path) -> None:
    goal_file = tmp_path / "empty.txt"
    goal_file.write_text("   \n\t\n", encoding="utf-8")
    with pytest.raises(GoalFileError) as exc:
        read_goal_from_file(str(goal_file))
    assert "empty" in str(exc.value)


# 功能：验证把目录当作 goal 文件时会报错而非抛出底层 IsADirectoryError
# 设计：直接传目录路径命中 IsADirectoryError 分支，断言消息含 "directory"，
#       覆盖用户误传目录这一常见错误输入
def test_read_goal_from_file_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(GoalFileError) as exc:
        read_goal_from_file(str(tmp_path))
    assert "directory" in str(exc.value)


# 功能：验证非 UTF-8 字节内容触发 UnicodeDecodeError 并被包装为 GoalFileError
# 设计：写入非法 UTF-8 字节（0xff），断言消息含 "UTF-8"，保证二进制文件不会静默乱码进 goal
def test_read_goal_from_file_invalid_utf8_raises(tmp_path: Path) -> None:
    goal_file = tmp_path / "binary.txt"
    goal_file.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(GoalFileError) as exc:
        read_goal_from_file(str(goal_file))
    assert "UTF-8" in str(exc.value)


# 功能：验证路径开头的 ~ 会被展开为用户主目录
# 设计：临时把 HOME 指向 tmp_path 并在其中放文件，传 "~/goal.txt"，断言能读到内容，
#       覆盖文档中给出的路径写法，不依赖真实 HOME 环境
def test_read_goal_from_file_expands_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "goal.txt").write_text("from home", encoding="utf-8")

    assert read_goal_from_file("~/goal.txt") == "from home"


# ── cmd_run with --file ──────────────────────────────────────────────────────


# 功能：验证 cmd_run 在传入 file 时读取文件内容作为 goal 发起 run
# 设计：monkeypatch _run_async 捕获收到的 goal 并返回 0，断言读到的就是文件内容且正常退出，
#       隔离网络/daemon 依赖，只验证 run.py 内部的“文件→goal→发起”逻辑
def test_cmd_run_reads_goal_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("  写一个冒泡排序  \n", encoding="utf-8")

    captured: dict[str, object] = {}

    async def fake_run_async(goal: str, config: LeaveConfig) -> int:
        captured["goal"] = goal
        return 0

    monkeypatch.setattr(run_mod, "_run_async", fake_run_async)

    with pytest.raises(SystemExit) as exc:
        cmd_run(None, LeaveConfig(), file=str(goal_file))

    assert exc.value.code == 0
    assert captured["goal"] == "写一个冒泡排序"


# 功能：验证 cmd_run 读取不存在的文件时报错并以退出码 1 结束
# 设计：断言 SystemExit 码为 1 且 stderr 含 "error:" 和 "not found"，
#       确保错误被 CLI 转换为可读提示与失败退出码，而不是抛未捕获异常
def test_cmd_run_missing_file_exits_with_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        cmd_run(None, LeaveConfig(), file=str(tmp_path / "missing.txt"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "not found" in err


# 功能：验证既没有 file 也没有有效 goal 时 cmd_run 拒绝执行并退出码 1
# 设计：分别传空字符串与 None，断言都会打印 "goal must not be empty" 且退出码 1，
#       覆盖 --goal 为空白这一防御性分支
def test_cmd_run_empty_goal_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cmd_run("   ", LeaveConfig())
    assert exc.value.code == 1
    assert "goal must not be empty" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc2:
        cmd_run(None, LeaveConfig())
    assert exc2.value.code == 1


# ── CLI 参数解析与分发 ────────────────────────────────────────────────────────


# 在 patch 掉配置/日志后运行 main，返回 cmd_run 实际收到的 {goal, file}
def _run_main_capturing(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, object]:
    monkeypatch.setattr(sys, "argv", ["leave", *argv])
    monkeypatch.setattr(main_mod, "get_config", lambda: LeaveConfig())
    monkeypatch.setattr(main_mod, "setup_logging", lambda config: None)

    seen: dict[str, object] = {}

    def fake_cmd_run(
        goal: str | None, config: LeaveConfig, *, file: str | None = None
    ) -> None:
        seen["goal"] = goal
        seen["file"] = file

    monkeypatch.setattr(main_mod, "cmd_run", fake_cmd_run)
    main_mod.main()
    return seen


# 功能：验证 main() 会把 run --file 解析结果透传给 cmd_run 的 file 参数
# 设计：patch sys.argv 与 main 命名空间下的 get_config/setup_logging/cmd_run，
#       捕获 cmd_run 的入参以确认“解析→分发”这条接线正确，这是本次改动的关键回归点
def test_main_dispatches_file_to_cmd_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("do it", encoding="utf-8")

    seen = _run_main_capturing(monkeypatch, ["run", "--file", str(goal_file)])

    assert seen == {"goal": None, "file": str(goal_file)}


# 功能：验证 run --goal 仍被解析并透传，保证新增 --file 未破坏旧用法
# 设计：走同一条 main→cmd_run 分发路径，断言 goal 有值而 file 为 None，锁定向后兼容
def test_main_dispatches_goal_to_cmd_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _run_main_capturing(monkeypatch, ["run", "--goal", "hello"])
    assert seen == {"goal": "hello", "file": None}


# 功能：验证 --goal 与 --file 同时出现时 CLI 报错并以码 2 退出
# 设计：两者互斥，捕获 argparse 抛出的 SystemExit 并断言 code==2，
#       确认互斥语义在真实入口处生效，而非仅存在于 cmd_run
def test_main_run_rejects_goal_and_file_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["leave", "run", "--goal", "x", "--file", "y"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2


# 功能：验证 run 不带任何目标来源参数时 CLI 直接报错并以码 2 退出
# 设计：互斥组 required=True 使 argparse 在解析阶段拒绝，断言 code==2，
#       确保缺少目标时不会静默进入 cmd_run
def test_main_run_requires_goal_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["leave", "run"])
    with pytest.raises(SystemExit) as exc:
        main_mod.main()
    assert exc.value.code == 2
