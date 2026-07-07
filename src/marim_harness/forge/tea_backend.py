"""The tea (Gitea CLI) ForgeBackend. This task adds only the *pure* pieces:
argv builders, tea-JSON->neutral-model mappers, and a JSON loader. The
subprocess I/O and the TeaBackend class arrive in the next task.

All values from ``tea … -o json --fields`` arrive as strings (``index:"51"``,
``mergeable:"false"``, ``ci:"success"``); the mappers coerce them.
"""

from __future__ import annotations

import json
from typing import Any

from .models import CiRun, ForgeError, PullRequest, normalize_ci

# The one field-rich PR endpoint that also carries `ci` and `mergeable`;
# `tea pr <n>` has a different, ci-less shape and is deliberately not used.
PR_FIELDS = "index,title,state,author,head,base,mergeable,url,updated,ci"


def _list_prs_args(state: str, limit: int) -> list[str]:
    return ["pr", "list", "--state", state, "--limit", str(limit),
            "-o", "json", "--fields", PR_FIELDS]


def _create_pr_args(title: str, body: str, base: str | None, draft: bool,
                    head: str) -> list[str]:
    args = ["pr", "create", "--head", head, "--title", title, "--description",
            body]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    return args


def _checkout_pr_args(number: int, create_branch: bool) -> list[str]:
    args = ["pr", "checkout", str(number)]
    if create_branch:
        args.append("-b")
    return args


def _runs_args() -> list[str]:
    return ["actions", "runs", "-o", "json"]


def _map_pr(obj: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=int(obj["index"]),
        title=obj.get("title", ""),
        state=obj.get("state", ""),
        author=obj.get("author", ""),
        head=obj.get("head", ""),
        base=obj.get("base", ""),
        mergeable=str(obj.get("mergeable", "")).strip().lower() == "true",
        url=obj.get("url", ""),
        updated=obj.get("updated", ""),
        ci=normalize_ci(obj.get("ci")),
    )


def _map_run(obj: dict[str, Any]) -> CiRun:
    return CiRun(
        workflow=obj.get("workflow", ""),
        status=obj.get("status", ""),
        event=obj.get("event", ""),
        branch=obj.get("branch", ""),
        started=obj.get("started", ""),
    )


def _loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        first = raw.strip().splitlines()[0] if raw.strip() else "<empty>"
        raise ForgeError(f"could not parse tea output: {first!r}") from exc
