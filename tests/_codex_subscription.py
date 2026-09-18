"""Closed OAuth/Responses fixtures; no real credentials or provider traffic."""

import asyncio
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import httpx2
import pytest

from marim_harness.config.model import ModelConfig, ModelSource
from marim_harness.runtime.builder import HarnessBuilder

ACCESS = "fixture-access-secret"
REFRESH = "fixture-refresh-secret"
MODEL = "gpt-6-astra"


def sse(*, text="done", tool=None, reasoning=None, input_tokens=100):
    suffix = uuid4().hex
    response = dict(
        id=f"resp_{suffix}",
        object="response",
        created_at=1,
        model=MODEL,
        status="in_progress",
        output=[],
        error=None,
        incomplete_details=None,
        moderation=None,
        background=False,
    )
    events = [{"type": "response.created", "response": response}]
    if reasoning:
        events.append(
            dict(
                type="response.reasoning_summary_text.delta",
                item_id=f"rs_{suffix}",
                output_index=0,
                summary_index=0,
                delta=reasoning,
            )
        )
    if tool:
        name, args = tool
        item = dict(
            type="function_call",
            id=f"fc_{suffix}",
            call_id=f"call_{suffix}",
            name=name,
            arguments=json.dumps(args),
            status="completed",
        )
        events.append(dict(type="response.output_item.added", output_index=1, item=item))
    else:
        events.append(
            dict(
                type="response.output_text.delta",
                item_id=f"msg_{suffix}",
                output_index=1,
                content_index=0,
                delta=text,
                logprobs=[],
            )
        )
    usage = dict(
        input_tokens=input_tokens,
        output_tokens=10,
        total_tokens=input_tokens + 10,
        input_tokens_details={"cached_tokens": 0},
        output_tokens_details={"reasoning_tokens": 0},
    )
    events.append(
        dict(
            type="response.completed", response={**response, "status": "completed", "usage": usage}
        )
    )
    return "".join(
        f"data: {json.dumps({**event, 'sequence_number': i})}\n\n" for i, event in enumerate(events)
    )


class Wire:
    def __init__(self):
        self.requests = []
        self.replies = []
        self.refreshes = []
        self.handler = None

    async def handle(self, request):
        body = await request.aread()
        record = {"url": str(request.url), "headers": dict(request.headers), "body": body.decode()}
        if str(request.url) == "https://auth.openai.com/oauth/token":
            self.refreshes.append(record)
        else:
            assert str(request.url) == "https://chatgpt.com/backend-api/codex/responses"
            record["json"] = json.loads(body)
            self.requests.append(record)
        if self.handler:
            return await self.handler(request)
        assert self.replies, f"Unexpected HTTP request: {request.url}: {body!r}"
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, tuple):
            status, payload = reply
            return httpx2.Response(status, json=payload)
        return httpx2.Response(200, headers={"Content-Type": "text/event-stream"}, content=reply)


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Every provider fixture uses a real upstream client over a closed transport."""
    from pydantic_ai.providers import openai_codex

    auth = tmp_path / "codex"
    auth.mkdir()
    (auth / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "account_id": "fixture-account",
                }
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(auth))
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "MARIM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    from marim_harness.config import model as config

    monkeypatch.setattr(config, "_codex_cli_available", lambda: False)
    monkeypatch.setattr(config, "_claude_cli_available", lambda: False)
    transport = Wire()
    monkeypatch.setattr(
        openai_codex,
        "create_async_httpx2_client",
        lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(transport.handle)),
    )

    async def no_network(*args, **kwargs):
        raise AssertionError("Unexpected live HTTP request")

    def no_sync_network(*args, **kwargs):
        raise AssertionError("Unexpected live HTTP request")

    for module in (httpx, httpx2):
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", no_network)
        monkeypatch.setattr(module.HTTPTransport, "handle_request", no_sync_network)
    original_popen = subprocess.Popen
    original_exec = asyncio.create_subprocess_exec

    def check_command(args):
        command = args if isinstance(args, str) else " ".join(map(str, args))
        assert "codex" not in command.lower(), "Unexpected Codex subprocess"

    def guarded_popen(args, *rest, **kwargs):
        check_command(args)
        return original_popen(args, *rest, **kwargs)

    async def guarded_exec(*args, **kwargs):
        check_command(args)
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)
    return transport


def source():
    return ModelSource(ModelConfig(provider="openai-codex", model=MODEL))


def harness(tmp_path, *, src=None, **kwargs):
    src = src or source()
    return (
        HarnessBuilder(workspace=tmp_path, model=src.build(MODEL))
        .with_config_overrides(
            model_source=src,
            model_id=f"openai-codex:{MODEL}",
            titler=None,
            **kwargs,
        )
        .build()
    )


def auth_path():
    import os

    return Path(os.environ["CODEX_HOME"]) / "auth.json"
