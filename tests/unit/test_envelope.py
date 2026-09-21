from __future__ import annotations

import pytest
from pydantic import ValidationError

from leave_claude.core.bus.envelope import (
    PARSE_ERROR,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)


def test_request_roundtrip() -> None:
    req = JsonRpcRequest(id="1", method="core.ping", params={"client": "test"})
    req2 = JsonRpcRequest.model_validate_json(req.model_dump_json())
    assert req2.id == "1"
    assert req2.method == "core.ping"
    assert req2.params == {"client": "test"}


def test_request_default_params() -> None:
    req = JsonRpcRequest(id="1", method="x")
    assert req.params == {}


def test_request_missing_id_raises() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "2.0", "method": "x"})


def test_request_wrong_version_raises() -> None:
    with pytest.raises(ValidationError):
        JsonRpcRequest.model_validate({"jsonrpc": "1.0", "id": "1", "method": "x"})


def test_success_roundtrip() -> None:
    resp = JsonRpcSuccess(id="1", result={"key": "value"})
    resp2 = JsonRpcSuccess.model_validate_json(resp.model_dump_json())
    assert resp2.id == "1"
    assert resp2.result == {"key": "value"}


def test_make_error_sets_code() -> None:
    err = make_error("1", PARSE_ERROR, "Parse error")
    assert err.error.code == PARSE_ERROR
    assert err.id == "1"
    assert err.error.data is None


def test_make_error_null_id() -> None:
    err = make_error(None, PARSE_ERROR, "bad json")
    assert err.id is None


def test_request_int_id_accepted() -> None:
    # 功能：验证 int 类型的 id 能通过 JSON-RPC 2.0 校验
    # 设计：规范允许 string/number/null,显式覆盖 number 以防回归
    req = JsonRpcRequest.model_validate({"jsonrpc": "2.0", "id": 42, "method": "x"})
    assert req.id == 42


def test_success_int_id_roundtrip() -> None:
    # 功能：验证 int id 的成功响应能 roundtrip 不失真
    # 设计：int 在 model_dump_json 后仍是 int,直接断言类型
    resp = JsonRpcSuccess(id=7, result={"ok": True})
    resp2 = JsonRpcSuccess.model_validate_json(resp.model_dump_json())
    assert resp2.id == 7
    assert isinstance(resp2.id, int)
