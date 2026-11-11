from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database


@dataclass
class DeliveryContext:
    task: dict[str, Any]
    artifacts: ArtifactStore
    db: Database
    agent: Any
    config: dict[str, Any]
    result: Any
    output_artifact: str
    registration: Any

    @property
    def task_id(self) -> str:
        return str(self.task["task_id"])

    @property
    def output_path(self):
        return self.artifacts.workspace / self.output_artifact

    def invoke_revision(self, feedback: str) -> Any:
        self.result = self.agent.invoke(
            {"messages": [{"role": "user", "content": feedback}]},
            config=self.config,
        )
        return self.result

    def read_output(self) -> str:
        return self.artifacts.read_text(self.output_artifact)


def validation_is_valid(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("valid", False))
    return bool(getattr(result, "valid", False))


def validation_errors(result: Any) -> list[str]:
    if isinstance(result, dict):
        errors = result.get("errors", [])
    else:
        errors = getattr(result, "errors", [])
    if isinstance(errors, str):
        return [errors]
    return [str(item) for item in (errors or [])]


def run_delivery_pipeline(registration, context: DeliveryContext) -> str:
    if registration.postprocessor is not None:
        outcome = registration.postprocessor(context)
        if isinstance(outcome, str) and outcome:
            context.output_artifact = outcome
        elif isinstance(outcome, dict) and outcome.get("output_artifact"):
            context.output_artifact = str(outcome["output_artifact"])

    if not context.artifacts.exists(context.output_artifact):
        raise RuntimeError(
            f"role '{registration.agent_type}' did not produce "
            f"'{context.output_artifact}'"
        )

    if registration.validator is not None:
        validation = registration.validator(
            context.read_output(), context.task, context.artifacts
        )
        if not validation_is_valid(validation):
            from app.runtime.errors import DeliveryValidationError
            errors = validation_errors(validation)
            raise DeliveryValidationError(
                "delivery validation failed: " + "; ".join(errors),
                feedback="; ".join(errors),
            )
        context.db.add_event(context.task_id, "delivery_validation_passed", {
            "agent_type": registration.agent_type,
            "output_artifact": context.output_artifact,
        })
    return context.output_artifact
