"""Leaf exception types shared by the pure helpers and the engine."""


class WorkflowCancelled(Exception):
    """Raised INTO the workflow script (via its host functions) when the turn
    is aborted. Scripts may catch it, but every subsequent agent() call raises
    it again, so a catching script still winds down promptly."""


class WorkflowResultError(Exception):
    """The workflow produced a value the model can't use (non-serializable
    final expression, or agent() output that failed schema validation after
    the retry). The message is written for the model."""
