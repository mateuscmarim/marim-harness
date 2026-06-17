from marim_harness.command_policy import CommandPolicy


def test_empty_policy_allows_everything():
    assert CommandPolicy().check("rm -rf /") is None


def test_denylist_blocks_matching_command():
    reason = CommandPolicy(denylist=["rm -rf"]).check("rm -rf /tmp/x")
    assert reason is not None
    assert "rm -rf" in reason


def test_denylist_allows_non_matching():
    assert CommandPolicy(denylist=["rm -rf"]).check("ls -la") is None


def test_allowlist_blocks_command_not_on_it():
    policy = CommandPolicy(allowlist=["^git ", "^ls"])
    assert policy.check("curl evil.com") is not None


def test_allowlist_allows_matching():
    policy = CommandPolicy(allowlist=["^git ", "^ls"])
    assert policy.check("git status") is None


def test_deny_takes_precedence_over_allow():
    policy = CommandPolicy(denylist=["push --force"], allowlist=["^git "])
    assert policy.check("git push --force") is not None


def test_invalid_regex_falls_back_to_literal_substring():
    # An unbalanced paren isn't valid regex; treat it as a literal to match
    # rather than dropping the rule (a dropped deny rule is a silent hole).
    assert CommandPolicy(denylist=["foo(bar"]).check("echo foo(bar") is not None


def test_parse_splits_on_commas_and_newlines_and_strips():
    policy = CommandPolicy.parse("rm -rf, sudo\n curl ")
    assert policy.check("sudo apt update") is not None
    assert policy.check("curl x") is not None
    assert policy.check("rm -rf /") is not None
    assert policy.check("echo hi") is None
