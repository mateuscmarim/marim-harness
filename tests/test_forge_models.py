from marim_harness.forge.models import CiStatus, ForgeError, PullRequest, normalize_ci


def test_normalize_ci_maps_known_and_unknown():
    assert normalize_ci("success") == "success"
    assert normalize_ci("SUCCESS") == "success"
    assert normalize_ci("failure") == "failure"
    assert normalize_ci("error") == "failure"
    assert normalize_ci("pending") == "pending"
    assert normalize_ci("") == "unknown"
    assert normalize_ci(None) == "unknown"
    assert normalize_ci("weird") == "unknown"


def test_pullrequest_is_frozen_with_defaults():
    pr = PullRequest(number=51, title="t", state="open", head="feat",
                     base="master", mergeable=True, url="http://x", ci="success")
    assert pr.author == "" and pr.updated == ""
    assert pr.number == 51 and pr.mergeable is True


def test_forge_error_is_exception():
    assert issubclass(ForgeError, Exception)
    assert str(ForgeError("boom")) == "boom"


def test_cistatus_defaults_empty_runs():
    assert CiStatus(overall="unknown").runs == ()
