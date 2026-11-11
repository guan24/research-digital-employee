from __future__ import annotations

from app.runtime.delivery import validation_errors, validation_is_valid
from app.runtime.errors import DeliveryValidationError
from app.runtime.report_validator import validation_feedback


MAX_REVISIONS = 2


def postprocess_research_delivery(context) -> str:
    """Research-owned bounded validation/repair; criteria remain in ReportValidator."""
    validator = context.registration.validator
    if validator is None:
        return context.output_artifact

    latest_feedback = ""
    for revision in range(MAX_REVISIONS + 1):
        report = context.read_output()
        validation = validator(report, context.task, context.artifacts)
        errors = validation_errors(validation)
        context.db.add_event(context.task_id, "report_validation_completed", {
            "valid": validation_is_valid(validation),
            "errors": errors,
            "source_count": getattr(validation, "source_count", 0),
            "word_count": getattr(validation, "word_count", 0),
            "evidence_coverage": getattr(validation, "evidence_coverage", 0.0),
            "cited_source_ids": list(getattr(validation, "cited_source_ids", ())),
            "finding_count": getattr(validation, "finding_count", 0),
            "revision": revision,
            "max_revisions": MAX_REVISIONS,
        })
        if validation_is_valid(validation):
            return context.output_artifact

        latest_feedback = validation_feedback(validation)
        if revision >= MAX_REVISIONS:
            raise DeliveryValidationError(
                "report validation failed after bounded revisions: " + "; ".join(errors),
                feedback=latest_feedback,
            )

        context.db.add_event(context.task_id, "report_revision_requested", {
            "revision": revision + 1,
            "feedback": latest_feedback[:3000],
            "repair_mode": "targeted",
        })
        context.invoke_revision(latest_feedback)

    raise DeliveryValidationError(
        "report validation failed after bounded revisions",
        feedback=latest_feedback,
    )
