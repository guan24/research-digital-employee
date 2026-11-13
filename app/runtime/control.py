"""Cooperative task-control signals shared by the runtime and tool services."""

class TaskControlSignal(RuntimeError):
    """Base class for cooperative task lifecycle signals."""


class TaskPaused(TaskControlSignal):
    """The operator requested a pause."""


class TaskCancelled(TaskControlSignal):
    """The operator requested cancellation or the task was removed."""


class TaskDeadlineExceeded(TaskControlSignal):
    """A non-negotiable safety ceiling was reached."""


class TaskStepLimitExceeded(TaskControlSignal):
    """A non-negotiable tool-call ceiling was reached."""


class WorkerLeaseLost(TaskControlSignal):
    """The current execution no longer owns its worker/task generation."""


class TaskWaitingForHuman(TaskControlSignal):
    """Execution was deliberately parked for an explicit human decision."""
