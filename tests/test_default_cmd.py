def test_think_flag_sets_env():
    from marim_harness.interfaces.cli.default_cmd import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["--think", "high"])
    assert args.think == "high"


def test_think_flag_choices_reject_unknown():
    import pytest as _pytest

    from marim_harness.interfaces.cli.default_cmd import _build_parser

    parser = _build_parser()
    with _pytest.raises(SystemExit):
        parser.parse_args(["--think", "ultra"])
