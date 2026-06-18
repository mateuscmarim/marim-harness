from marim_harness.lsp.diagnostics import DiagnosticsCollector, format_diagnostics


class _FakeServer:
    """Stands in for multilspy's LanguageServer: exposes a nested ``server``
    handler with ``on_notification(method, handler)``."""

    def __init__(self):
        self._handlers = {}

        class _Handler:
            def on_notification(_self, method, handler):
                self._handlers[method] = handler

        self.server = _Handler()

    def publish(self, uri, diags):
        self._handlers["textDocument/publishDiagnostics"]({"uri": uri, "diagnostics": diags})


def test_collector_attaches_and_collects():
    srv = _FakeServer()
    c = DiagnosticsCollector()
    c.attach(srv)
    assert c.enabled is True
    srv.publish("file:///x/a.py", [{"severity": 1, "message": "boom"}])
    assert c.latest("file:///x/a.py") == [{"severity": 1, "message": "boom"}]
    assert c.latest("file:///x/missing.py") == []


def test_collector_disabled_when_no_handler_api():
    class Bare:
        server = object()  # no on_notification

    c = DiagnosticsCollector()
    c.attach(Bare())
    assert c.enabled is False


def test_collector_handler_tolerates_extra_leading_arg():
    srv = _FakeServer()
    c = DiagnosticsCollector()
    c.attach(srv)
    # Simulate a handler(server, params) call shape.
    c._on_publish(
        object(), {"uri": "file:///y.py", "diagnostics": [{"severity": 2, "message": "warn"}]}
    )
    assert c.latest("file:///y.py") == [{"severity": 2, "message": "warn"}]


def test_format_no_diagnostics():
    assert format_diagnostics("a.py", []) == "a.py: no diagnostics"


def test_format_lists_severity_and_position():
    diags = [
        {
            "severity": 1,
            "message": "undefined name",
            "range": {"start": {"line": 4, "character": 2}},
        },
        {
            "severity": 2,
            "message": "unused import\nsecond line",
            "range": {"start": {"line": 0, "character": 0}},
        },
    ]
    out = format_diagnostics("a.py", diags)
    assert "a.py:5:3: error: undefined name" in out
    assert "a.py:1:1: warning: unused import" in out
    assert "second line" not in out  # only first message line kept


def test_format_truncates():
    diags = [
        {"severity": 1, "message": f"m{i}", "range": {"start": {"line": i, "character": 0}}}
        for i in range(60)
    ]
    out = format_diagnostics("a.py", diags, max_results=10)
    assert "… and 50 more" in out
