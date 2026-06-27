"""Precision of the textual transient-marker matching in ``errors.py``.

The old implementation matched bare substrings "502"/"503"/"504"/"timeout"
against the lowercased upstream body, so unrelated numbers (byte counts, ids,
timestamps) falsely classified a genuine 400 bad-request as transient → a
spurious retry. The status-code markers now require a status-ish context (or the
canonical reason phrase) and a standalone digit token, while distinctive phrases
("timed out", "overloaded", ...) still match as before.
"""

from pydantic_ai.exceptions import ModelHTTPError

from marim_harness.runtime.errors import is_transient_model_error


def _http_error(status: int, body=None) -> ModelHTTPError:
    return ModelHTTPError(status_code=status, model_name="m", body=body)


def test_unrelated_number_containing_502_is_not_transient():
    """A genuine 400 whose message merely contains the digits 502/503/504 inside
    a larger number must NOT be retried."""
    body = {
        "message": "Invalid request: field 'max_tokens' value 65029 exceeds limit",
        "code": 400,
        "metadata": {"raw": "request id req-503812-aa rejected after 504000 bytes"},
    }
    assert is_transient_model_error(_http_error(400, body)) is False


def test_status_named_in_prose_is_transient():
    """A 502/503/504 named in a status-ish context still classifies transient."""
    body = {
        "message": "Provider returned error",
        "code": 400,
        "metadata": {"raw": "upstream responded with status 503"},
    }
    assert is_transient_model_error(_http_error(400, body)) is True


def test_canonical_reason_phrase_is_transient():
    body = {
        "message": "502 Bad Gateway from upstream",
        "code": 400,
    }
    assert is_transient_model_error(_http_error(400, body)) is True


def test_distinctive_phrase_still_transient():
    body = {
        "message": "Provider returned error",
        "code": 400,
        "metadata": {"raw": "the model is currently overloaded, please try again"},
    }
    assert is_transient_model_error(_http_error(400, body)) is True
