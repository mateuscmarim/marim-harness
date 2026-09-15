"""Validation failure at the runner-to-workflow report boundary."""


class WorkflowResultError(ValueError):
    """A workflow binding's schema or the worker's full report is invalid."""
