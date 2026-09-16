"""Durable, transport-neutral claims for opt-in HTTP mutations.

A claim is committed before the effect and never deleted. A process cannot
atomically commit an arbitrary harness effect with this ledger: if it dies in
between, the pending record deliberately refuses execution forever. Completed
responses survive restarts; this provides safe retries, not exactly-once effects.
"""

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

MAX_RESPONSE_BYTES = 1024 * 1024


def valid_key(value: str) -> bool:
    """Require a single canonical, bounded UUID, not an arbitrary client string."""
    if len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def request_fingerprint(method: str, path: str, query: bytes, body: bytes) -> str:
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query, body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: bytes
    headers: list[tuple[bytes, bytes]]


Claim = Literal["claimed", "conflict", "in_progress", "unknown"] | StoredResponse


class OperationStore:
    """Short SQLite transactions; no request payloads or credentials are stored.

    Separate connections and SQLite's write lock serialize claims across processes.
    The small local lock makes the active set consistent with transactions across
    threads. It is intentionally ephemeral: only this store's executing handlers
    can promise that a pending operation is still in progress.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        # Lazy initialization lets reads and legacy routes continue working when
        # storage is unavailable, while protected routes fail closed on claim.
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._directory.chmod(0o700)
        path = self._directory / "operations.sqlite3"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        connection = sqlite3.connect(path, timeout=1.0)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operations ("
                "key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                "status INTEGER, body BLOB, headers TEXT)"
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def claim(self, key: str, fingerprint: str) -> Claim:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, status, body, headers FROM operations WHERE key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                return self._existing(key, fingerprint, row)
            connection.execute(
                "INSERT INTO operations (key, fingerprint) VALUES (?, ?)", (key, fingerprint)
            )
            # Commit before exposing a claim to the caller. A commit failure may
            # itself be ambiguous, but the caller never executes the operation.
            connection.commit()
            self._active.add(key)
            return "claimed"

    def _existing(self, key: str, fingerprint: str, row: tuple) -> Claim:
        saved_fingerprint, status, body, headers = row
        if saved_fingerprint != fingerprint:
            return "conflict"
        if status is not None:
            pairs = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in json.loads(headers)]
            return StoredResponse(status, body, pairs)
        return "in_progress" if key in self._active else "unknown"

    def complete(self, key: str, response: StoredResponse) -> None:
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise ValueError("mutation response exceeds durable response limit")
        headers = json.dumps(
            [(k.decode("latin-1"), v.decode("latin-1")) for k, v in response.headers]
        )
        with self._lock, closing(self._connect()) as connection, connection:
            updated = connection.execute(
                "UPDATE operations SET status = ?, body = ?, headers = ? "
                "WHERE key = ? AND status IS NULL",
                (response.status, response.body, headers, key),
            )
            if updated.rowcount != 1:
                raise ValueError("pending operation record is unavailable")
            connection.commit()
            self._active.discard(key)

    def abandon(self, key: str) -> None:
        """An exception/cancellation leaves the durable pending record untouched."""
        with self._lock:
            self._active.discard(key)
