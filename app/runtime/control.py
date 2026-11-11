"""Cooperative task-control signals and hard resource gates."""


class TaskControlSignal(RuntimeError):
    """Base class for task lifecycle and resource-control signals."""


class TaskPaused(TaskControlSignal):
    """The operator requested a pause."""


class TaskCancelled(TaskControlSignal):
    """The operator requested cancellation or the task was removed."""


class TaskDeadlineExceeded(TaskControlSignal):
    """A non-negotiable safety deadline was reached."""


class TaskStepLimitExceeded(TaskControlSignal):
    """The task-wide tool-call hard limit was reached."""


class WorkerLeaseLost(TaskControlSignal):
    """The current execution no longer owns its worker/task generation."""


class BudgetExceeded(TaskControlSignal):
    """A preflight resource gate rejected work before it was started."""

    def __init__(self, code: str, dimension: str, message: str, details=None):
        super().__init__(message)
        self.code = code
        self.dimension = dimension
        self.details = details or {}
