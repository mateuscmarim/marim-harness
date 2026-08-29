# Quality gate

A **ratchet**. It reads what the tools already wrote, compares each number to
`quality-baseline.json`, and fails the run if any of them got worse. There is no
threshold to reach: the baseline starts wherever the code is, improvements are
adopted automatically after merge, and the bar only ever rises.

The program is [`ratchet-gate`](https://git.marim.dev/mateuscmarim/quality-gate),
installed per-run with `uvx` and pinned in the workflow. Nothing is vendored
here — this repo carries only what legitimately differs: `quality-gate.toml`,
`quality-baseline.json`, `.gitleaks.toml` and `.gitea/workflows/quality-gate.yml`.

## It does not replace `ci.yml`

The two enforce different things, and the split is deliberate:

| | enforces | how it fails |
|---|---|---|
| `ci.yml` | `ruff check`, `ruff format --check`, `pyright`, the suite — on 3.10 / 3.12 / 3.14 | hard, at zero |
| `quality-gate.yml` | coverage, complexity, bandit, pip-audit, secrets — on 3.12 | a metric moved the wrong way |

Anything `ci.yml` already pins at zero (`lint_errors`, `format_drift`) still
appears as a gate row so the summary table is self-contained — but the hard
failure is what actually holds those at zero. Treat those rows as bookkeeping.

## What is scored

| Metric | Baseline at seeding | Notes |
|---|---:|---|
| `coverage_lines_pct` | 92.4 | ↑, ±0.1 tolerance |
| `coverage_branches_pct` | 87.17 | ↑, ±0.1 |
| `lint_errors` | 0 | already hard-enforced |
| `complexity_violations` | 61 | `C901` + `PLR0911/0912/0913/0915`, `src` only |
| `bandit_high` / `bandit_medium` | 0 / 0 | 45 LOW findings are not scored |
| `format_drift` | 0 | already hard-enforced |
| `pip_audit_findings` | 9 | repo-wide, severity-agnostic |
| `secret_findings` | 0 | `boolean_must_be_zero` — no budget, ever |

There is **no `mypy_errors` row.** This repo type-checks with pyright, which
`ci.yml` enforces at zero — stricter than any ratchet. `ratchet-gate` ships no
pyright parser, and adding mypy purely to feed the row would mean a second type
checker with its own config and a large starting error count.

`complexity_violations` is the one row measuring something no other check does:
`pyproject.toml` selects only `C901`, so the 48 `PLR0913` (too many arguments)
and 13 `PLR0911` (too many returns) findings in `src/` were never enforced. A
further 30 in `tests/` are excluded by `non_ratcheted_dirs`; they stay visible
in the uploaded artifact, they just do not gate.

## Running it locally

```bash
mkdir -p .quality/marim_harness
out=.quality/marim_harness
uv run ruff check src tests --output-format=json > $out/ruff.json || true
uv run ruff check --select C901,PLR0911,PLR0912,PLR0913,PLR0915 --no-cache \
  --output-format=json src tests > $out/ruff-complexity.json || true
uv run ruff format --check src tests && echo 0 > $out/format.txt || echo 1 > $out/format.txt
uv run pytest --cov-branch --cov-report=json:$out/coverage.json || true
uvx bandit -r src -f json -o $out/bandit.json -q || true
uv pip freeze > /tmp/freeze.txt
uvx pip-audit -f json -r /tmp/freeze.txt -o .quality/pip-audit.json || true
gitleaks detect --config .gitleaks.toml --report-format=json \
  --report-path .quality/gitleaks.json --no-banner --redact

uvx ratchet-gate@0.5.0 check          # exit 1 on any regression
```

`.quality/` is gitignored. A bare `check` scores against this checkout's own
baseline; CI adds `--baseline-ref origin/master` on PRs so a branch cannot
raise its own bar in the same commit that regresses.

## Three jobs, and why

| Job | Permissions | Runs repository code? |
|---|---|---|
| `gate` | `contents: read` | **Yes** — `uv sync` and `pytest` |
| `report` | `pull-requests: write` | No — downloads the summary, posts a comment |
| `promote` | `contents: write` | No — master only, after the merge |

The split is a security boundary, not tidiness. `gate` runs the pull request's
own code, so it holds no write scope and cannot push anywhere. Everything that
needs to write is in a job that runs no repository code, and `promote` is
additionally reachable only from a push to `master`. Keep it that way: moving
the promote steps back into `gate` would hand every PR author a write-scoped
token inside a process they control.

On the PR that first adds `quality-baseline.json` the workflow probes for the
file on `origin/master` and omits the flag when it is absent — the gate fails
closed on an unreadable reference, which would otherwise make the adoption PR
impossible to pass. The probe expires by itself once the baseline is on master.

### Expect the coverage rows to read low locally

**Seed and re-seed coverage from a CI run, not from your machine.** The Gitea
runner executes as root, and five tests skip on `geteuid() == 0` because `chmod`
cannot provoke a permission failure for root (two in `test_history.py`, three in
`test_memory.py`). Their lines are never covered in CI, so a laptop measures
about 0.1 higher than the job that actually scores every run — enough to blow the
0.1 tolerance. The baseline was seeded locally once and failed the gate's first
real run for exactly this reason.

A local `check` showing coverage a shade *above* baseline is therefore normal and
not an improvement to promote. It also means CI never exercises those
permission-failure paths; closing that gap means running the job as a non-root
user, which is a runner change, not a gate change.

The same rule covers the interpreter: the gate job pins the 3.12 *series*,
and the runner currently supplies 3.12.3. Do not record an exact patch
anywhere without re-measuring on it — the baseline once claimed 3.12.13,
which no runner has ever run.

## The secrets allowlist

The first scan reported **1345** `generic-api-key` hits — every one a provider
key *fixture* (`sk-or-test-1234abcd`, `gm-key-12345678`) re-counted across each
commit that touched one of three files. `secret_findings` is scored
`boolean_must_be_zero`, so that would have failed the gate permanently.

`.gitleaks.toml` allowlists those three paths. It is scoped by **path, not by
value**: matching the stub patterns themselves would silently cover any future
file using the same shape. Naming the files means a new fixture file fails the
gate until someone decides which it is — a stub to allowlist, or a real key that
just got caught. Note that `non_ratcheted_dirs` does **not** apply here; global
metrics are counted unscoped.

## After a merge

`promote --monotonic` runs on master pushes and commits the result as
`quality-gate-bot`. Monotonic mode can only tighten, so it is safe unattended
and its own commit re-runs to a no-op. It runs under `always()` on purpose: if
it inherited the gate's failure, one stuck red row would freeze promotion for
every *other* metric, and the ratchet would quietly stop ratcheting.

To adopt improvements by hand: `uvx ratchet-gate@0.5.0 promote --monotonic`.
Never plain `promote` on a branch — without `--monotonic` it sets baseline =
current for *every* metric, so seeding one new row rewrites all the others.
