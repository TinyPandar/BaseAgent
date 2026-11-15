"""Exceptions whose class names Harbor's error policy keys on (``ExceptionInfo.exception_type``)."""


class BudgetExceededError(Exception):
    """The CNY budget stopped the run.

    Harbor 0.26.0 already treats this name as: grade the trial, reward invalid, no retry.
    The generated job config repeats that explicitly in ``override_error_policy``.
    """


class BaseAgentInfrastructureError(Exception):
    """The environment, provider or harness failed; not a model-quality outcome.

    The job config maps this name to should_grade=false, has_valid_reward=false and
    no retry, so Harbor never counts it as a wrong answer.
    """
