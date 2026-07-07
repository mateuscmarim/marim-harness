import pytest

from marim_harness.forge import tea_backend as tb
from marim_harness.forge.models import ForgeError

PR_JSON = """[
  {"index": "51", "title": "refactor tools", "state": "merged", "author": "Mateus",
   "head": "refactor/tools", "base": "master", "mergeable": "false",
   "url": "https://git.marim.dev/x/pulls/51", "updated": "2026-07-05T09:21:25Z", "ci": "success"}
]"""

RUNS_JSON = """[
  {"id": "1093", "status": "completed", "workflow": "feat install", "branch": "master",
   "event": "push", "started": "2026-07-07T16:01:42Z", "duration": "8m"}
]"""


def test_list_prs_args_includes_fields_and_json():
    args = tb._list_prs_args("open", 30)
    assert args[:2] == ["pr", "list"]
    assert "--state" in args and "open" in args
    assert "--limit" in args and "30" in args
    assert "-o" in args and "json" in args
    assert "--fields" in args and tb.PR_FIELDS in args
    assert all(isinstance(a, str) for a in args)  # argv list, injection guard


def test_create_pr_args_optional_flags():
    base = tb._create_pr_args("T", "B", None, False, "feat")
    assert "--head" in base and "feat" in base
    assert "--title" in base and "T" in base
    assert "--description" in base and "B" in base
    assert "--base" not in base and "--draft" not in base
    full = tb._create_pr_args("T", "B", "master", True, "feat")
    assert "--base" in full and "master" in full
    assert "--draft" in full


def test_checkout_pr_args():
    assert tb._checkout_pr_args(7, True) == ["pr", "checkout", "7", "-b"]
    assert tb._checkout_pr_args(7, False) == ["pr", "checkout", "7"]


def test_map_pr_coerces_types_and_normalizes_ci():
    pr = tb._map_pr(tb._loads(PR_JSON)[0])
    assert pr.number == 51 and isinstance(pr.number, int)
    assert pr.mergeable is False
    assert pr.ci == "success"
    assert pr.head == "refactor/tools" and pr.base == "master"
    assert pr.url.endswith("/pulls/51")


def test_map_run_fields_and_none_conclusion():
    run = tb._map_run(tb._loads(RUNS_JSON)[0])
    assert run.workflow == "feat install"
    assert run.status == "completed"
    assert run.event == "push" and run.branch == "master"
    assert run.conclusion is None and run.url is None


def test_loads_raises_forgeerror_on_bad_json():
    with pytest.raises(ForgeError) as exc:
        tb._loads("not json{")
    assert "could not parse" in str(exc.value)
