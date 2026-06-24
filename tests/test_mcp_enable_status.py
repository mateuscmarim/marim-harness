"""``McpManager.enable_server`` status bookkeeping on a failed reconnect.

On a failed ``_connect_one`` the manager used to return the error but never
record it: ``mcp_status["failed"]`` stayed empty and a stale ``"connected"``
entry from an earlier session lingered. enable_server now mirrors connect()'s
bookkeeping so the status stays accurate.
"""

import pytest

from marim_harness.mcp.manager import McpManager


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _OkServer:
    id = "ok"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FailServer:
    id = "boom"

    async def __aenter__(self):
        raise RuntimeError("transport refused")

    async def __aexit__(self, *exc):
        return False


@pytest.mark.anyio
async def test_enable_failed_reconnect_records_failure(tmp_path):
    mgr = McpManager([_FailServer()], {"boom"})
    err = await mgr.enable_server("boom", tmp_path)
    assert err is not None and "transport refused" in err
    assert mgr.mcp_status["failed"] == [("boom", err)]
    assert "boom" not in mgr.mcp_status["connected"]


@pytest.mark.anyio
async def test_enable_failed_reconnect_clears_stale_connected_entry(tmp_path):
    mgr = McpManager([_FailServer()], set())
    # Simulate a stale "connected" entry from a prior session.
    mgr.mcp_status["connected"] = ["boom"]
    mgr.disabled.add("boom")

    err = await mgr.enable_server("boom", tmp_path)
    assert err is not None
    assert "boom" not in mgr.mcp_status["connected"]
    assert mgr.mcp_status["failed"] == [("boom", err)]


@pytest.mark.anyio
async def test_enable_failed_reconnect_does_not_duplicate_failure(tmp_path):
    mgr = McpManager([_FailServer()], {"boom"})
    await mgr.enable_server("boom", tmp_path)
    mgr.disabled.add("boom")  # re-disable so the second enable re-attempts connect
    await mgr.enable_server("boom", tmp_path)
    # Exactly one entry for the server, not appended twice.
    assert [f for f in mgr.mcp_status["failed"] if f[0] == "boom"].__len__() == 1


@pytest.mark.anyio
async def test_enable_success_still_records_connected(tmp_path):
    mgr = McpManager([_OkServer()], {"ok"})
    err = await mgr.enable_server("ok", tmp_path)
    assert err is None
    assert "ok" in mgr.mcp_status["connected"]
    assert all(f[0] != "ok" for f in mgr.mcp_status["failed"])
