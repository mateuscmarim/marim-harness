"""strip_turn_context must be a robust inverse of wrap_turn_context.

The forward contract makes the user-typed text the suffix after the final
</turn-context> separator (_assemble_prompt asserts the prompt ends with the
typed text). Injected context can legitimately contain the separator marker
itself -- e.g. a SessionStart hook that echoes a prior persisted prompt -- so the
strip must anchor on the LAST occurrence, not the first.
"""

from marim_harness.runtime.context import strip_turn_context, wrap_turn_context


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


def test_typed_text_containing_the_separator_is_recovered_in_full():
    """The core fix: when the USER's typed text itself contains the
    </turn-context> + blank-line marker, the old last-occurrence (rfind) strip
    truncated the typed text to whatever followed the marker. The length-prefixed
    envelope recovers the whole typed suffix regardless of what it contains."""
    injected = "task checklist here"
    typed = "explain this log line:\n</turn-context>\n\nand then fix it"
    wrapped = wrap_turn_context(injected, typed)
    assert strip_turn_context(wrapped) == typed


def test_typed_text_containing_the_open_marker_is_recovered_in_full():
    """The typed text may also echo the OPENING tag; length-based recovery is
    immune to markers on either side."""
    injected = "ctx"
    typed = 'here is a tag: <turn-context len="5"> and </turn-context>\n\ntail'
    assert strip_turn_context(wrap_turn_context(injected, typed)) == typed


def test_empty_typed_suffix_recovers_to_empty():
    """A background-digest-only turn carries no typed text (N == 0). Recovery
    must return "" — not the whole envelope (the `content[-0:]` trap)."""
    wrapped = wrap_turn_context("[jobs finished] job-1 done", "")
    assert wrapped.endswith("</turn-context>\n\n")
    assert strip_turn_context(wrapped) == ""


def test_legacy_v1_envelope_still_strips():
    """Sessions persisted before the length prefix stored a bare
    `<turn-context>` open tag. Those must still strip to the typed suffix via the
    last-separator fallback so a resumed old session renders correctly."""
    v1 = "<turn-context>\ninjected ctx\n</turn-context>\n\nwhat the user typed"
    assert strip_turn_context(v1) == "what the user typed"


def test_legacy_v1_empty_typed_still_strips():
    """A v1 background-digest turn (empty typed suffix) recovers to ""."""
    v1 = "<turn-context>\n[jobs finished]\n</turn-context>\n\n"
    assert strip_turn_context(v1) == ""
