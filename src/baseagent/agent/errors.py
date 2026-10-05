class WorkspaceChanged(ValueError):
    """Observed files or repository guidance changed outside known tool writes."""


class BudgetExceeded(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status.value)


class TaskPaused(BaseException):
    """Delegation control flow must not become an ordinary tool failure."""

    def __init__(self, task_id, status):
        self.task_id, self.status = task_id, status
