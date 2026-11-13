"""How a step fails."""


class StepError(Exception):
    """The step cannot finish. Its message says why, for the turn's log; the step exits non-zero."""
