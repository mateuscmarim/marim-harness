"""The ForgeBackend seam: the interface every concrete forge CLI satisfies.

A Protocol (not a base class) — a backend just needs these five async methods
returning neutral models. Adding a backend (e.g. gh) changes nothing else in the
system: not the models, not the tools, not the wiring."""

from __future__ import annotations

from typing import Protocol

from .models import CiStatus, PullRequest


class ForgeBackend(Protocol):
    async def list_prs(self, state: str, limit: int) -> list[PullRequest]: ...

    async def find_open_pr_for_branch(self, branch: str) -> PullRequest | None: ...

    async def view_pr(self, number: int | None, branch: str | None) -> PullRequest | None: ...

    async def ci_status(self, branch: str) -> CiStatus: ...

    async def create_pr(
        self, title: str, body: str, base: str | None, draft: bool, head: str
    ) -> PullRequest: ...

    async def checkout_pr(self, number: int, create_branch: bool) -> str: ...
