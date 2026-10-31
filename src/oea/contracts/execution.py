"""Execution bounds shared by Run and Workflow recovery."""

from pydantic import Field, ValidationError

from oea.contracts.configuration import Configuration
from oea.contracts.errors import RuntimeFault
from oea.contracts.schema_validation import parameter_errors


class ResumeExecutionLimits(Configuration):
    max_steps: int | None = Field(default=None, strict=True, ge=1, le=100000)


def resume_limits(value):
    if value is None:
        return {}
    try:
        return ResumeExecutionLimits.model_validate(value).model_dump(exclude_none=True)
    except ValidationError as error:
        errors = parameter_errors(error)
        for item in errors:
            item["path"].insert(0, "execution_limits")
        raise RuntimeFault("INVALID_REQUEST", "Execution limits do not match the recovery contract", origin="local",
                           stage="operation.validate", details={"errors": errors, "fields": [item["path"] for item in errors]}) from None


def agent_steps(execution, budget):
    return execution.max_steps or (budget["max_model_calls"] * 4 + 16 if execution.graph == "default" else 512)


def step_limit_fault(limit, graph, version, checkpoint, **owner):
    return RuntimeFault("EXECUTION_STEP_LIMIT", "The graph reached its execution step limit", origin="local", stage="graph.execute",
                        details={"effective_max_steps": limit, "unit": "super_step", "graph": graph, "graph_version": version},
                        evidence_ref={"checkpoint": checkpoint, **owner},
                        continuation={"operation": "workflows.resume" if "workflow_run_id" in owner else "runs.resume",
                                      "condition": "Inspect original receipts and remaining budget before adjusting execution_limits"}, **owner)
