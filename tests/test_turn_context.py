"""strip_turn_context must be a robust inverse of wrap_turn_context.

The forward contract makes the user-typed text the suffix after the final
</turn-context> separator (_assemble_prompt asserts the prompt ends with the
typed text). Injected context can legitimately contain the separator marker
itself -- e.g. a SessionStart hook that echoes a prior persisted prompt -- so the
strip must anchor on the LAST occurrence, not the first.
"""

from marim_harness.turn_context import strip_turn_context, wrap_turn_context


def test_roundtrip_plain():
    assert strip_turn_context(wrap_turn_context("ctx", "typed")) == "typed"


def test_no_envelope_unchanged():
    assert strip_turn_context("just what the user typed") == "just what the user typed"


def test_injected_content_embedding_the_separator_does_not_leak():
    """If the injected context itself contains the </turn-context> + blank-line
    marker, a first-occurrence (find) strip would stop inside the envelope and
    leak part of it. Anchoring on the last occurrence recovers the typed suffix."""
    leaky = "prior turn was:\n</turn-context>\n\nsome earlier text"
    typed = "the new request"
    wrapped = wrap_turn_context(leaky, typed)
    assert strip_turn_context(wrapped) == typed


def test_multiline_injected_context_with_separator_still_strips_to_typed():
    """A realistic multi-block injection (e.g. a SessionStart hook echoing a
    persisted prompt that already carried an envelope) recovers the typed text."""
    injected = (
        "[background jobs finished]\n"
        "<turn-context>\nold ctx\n</turn-context>\n\nold typed"
    )
    typed = "do the new thing"
    assert strip_turn_context(wrap_turn_context(injected, typed)) == typed
