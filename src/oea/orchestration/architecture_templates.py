"""Ordinary editable architecture declarations, never hidden planning engines."""

from oea.contracts.architecture import ArchitectureBuilder


def model_stage_routes(graph, identities):
    """Each additional model has explicit tool rejection, recovery and wait paths."""
    for identity in identities:
        edge = next(edge for edge in graph.value["edges"] if edge["source"] == identity)
        edge["branch"] = "deliver"
        for kind in ("tools", "recover_context", "wait"):
            name = identity + "_" + kind
            graph.node(name, kind, title=kind.replace("_", " ").title(), join="any")
            graph.connect(identity, name, branch=kind).connect(name, identity)


def react():
    graph = ArchitectureBuilder()
    for identity, kind, title, description in (
        ("prepare", "prepare", "Prepare", "Load the authorized context and work intent"),
        ("model", "model", "Decide", "Choose a tool or produce the requested result"),
        ("tools", "tools", "Act", "Execute calls through durable authorized receipts"),
        ("observe", "observe", "Observe", "Continue with the original tool observations"),
        ("recover_context", "recover_context", "Recover context", "Reduce input after a confirmed context rejection"),
        ("wait", "wait", "Wait", "Suspend durably for input or dependencies"),
        ("deliver", "deliver", "Deliver", "Retain the complete result and check explicit requirements")):
        graph.node(identity, kind, title=title, description=description, join="any")
    graph.connect("prepare", "model")
    for branch in ("tools", "deliver", "wait", "recover_context"):
        graph.connect("model", branch, branch=branch)
    graph.connect("recover_context", "model").connect("tools", "observe")
    graph.connect("observe", "model", branch="model").connect("observe", "wait", branch="wait")
    graph.connect("wait", "model")
    return graph.build(source={"template": "react", "version": 1})


def plan_execute():
    graph = ArchitectureBuilder(react())
    graph.state("plan_ref", type="string").state("step_ref", type="string")
    graph.node("plan", "model", title="Plan", description="Write a bounded ordered plan from the goal and existing evidence", tools=False,
               instruction="Plan the current task in a short numbered sequence. Preserve completed work when replanning. Do not execute tools in this planning stage.", join="any")
    graph.node("save_plan", "remember", title="Save plan", label="plan", outputs={"reference": "plan_ref"})
    graph.node("save_step", "remember", title="Save step result", label="step-result", outputs={"reference": "step_ref"})
    graph.node("plan_done", "condition", field="response_id", response_text=True, operator="contains", value="PLAN_DONE")
    graph.node("replan", "condition", field="response_id", response_text=True, operator="contains", value="REPLAN")
    for node in graph.value["nodes"]:
        if node["id"] == "model":
            node["title"] = "Execute next step"
            node["config"] = {"instruction": "Execute the next unfinished step of the saved plan using evidence and tools. Once a step finishes, report its result. Include PLAN_DONE only when all work is complete; include REPLAN only when evidence requires a revised plan. Do not repeat completed side effects."}
    graph.value["edges"] = [edge for edge in graph.value["edges"] if edge["source"] != "prepare" and not (edge["source"] == "model" and edge["branch"] == "deliver")]
    graph.connect("prepare", "plan").connect("plan", "save_plan").connect("save_plan", "model")
    graph.connect("model", "save_step", branch="deliver").connect("save_step", "plan_done")
    graph.connect("plan_done", "deliver", branch="true").connect("plan_done", "replan", branch="false")
    graph.connect("replan", "plan", branch="true").connect("replan", "model", branch="false")
    model_stage_routes(graph, ("plan",))
    return graph.build(source={"template": "plan_execute", "version": 1}, metadata={"preview_stages": ["prepare", "plan", "model", "tools", "deliver"]})


def reflexion():
    graph = ArchitectureBuilder(react())
    graph.state("attempts", type="integer", reducer="sum", default=0)
    graph.state("answer_ref", type="string").state("feedback_ref", type="string").state("experience_ref", type="string")
    graph.state("answer_id", type="string")
    graph.node("save_answer", "remember", title="Save attempt", label="attempt", outputs={"reference": "answer_ref"})
    graph.node("keep_answer", "set", values={"answer_id": "$response_id"})
    graph.node("evaluate", "model", title="Evaluate", description="Evaluate the current result against the goal", tools=False,
               instruction="Evaluate the preceding task result against the request and available evidence. Give specific feedback. Include ACCEPT_RESULT only if the result meets the request; otherwise explain the defect. Do not execute tools.")
    graph.node("save_feedback", "remember", title="Save feedback", label="feedback", outputs={"reference": "feedback_ref"})
    graph.node("accepted", "condition", field="response_id", response_text=True, operator="contains", value="ACCEPT_RESULT")
    graph.node("bounded", "condition", field="attempts", operator="ge", value=2)
    graph.node("reflect", "model", title="Reflect", description="Turn feedback into concrete guidance for the next attempt", tools=False,
               instruction="Reflect on the saved evaluation and previous attempt. Record concise actionable lessons for the next attempt. Preserve verified results and original tool receipts; never repeat an unknown side effect.")
    graph.node("save_experience", "remember", title="Save experience", label="experience", outputs={"reference": "experience_ref"})
    graph.node("next_attempt", "set", values={"attempts": 1})
    graph.node("restore_answer", "set", values={})
    # The model's final response is selected by a mapped input at delivery;
    # the evaluation and reflection remain separate durable responses.
    for node in graph.value["nodes"]:
        if node["id"] == "deliver":
            node["config"] = {"response_field": "answer_id"}
        elif node["id"] == "model":
            node["config"] = {"instruction": "Complete the original task using the available tools and evidence. On later attempts apply the saved evaluation and reflection to produce an improved task result. Preserve verified work and original tool receipts; do not repeat completed or unknown side effects. Produce the task result in this stage, rather than another evaluation or reflection."}
    graph.value["edges"] = [edge for edge in graph.value["edges"] if not (edge["source"] == "model" and edge["branch"] == "deliver")]
    graph.connect("model", "save_answer", branch="deliver").connect("save_answer", "keep_answer").connect("keep_answer", "evaluate")
    graph.connect("evaluate", "save_feedback").connect("save_feedback", "accepted")
    graph.connect("accepted", "restore_answer", branch="true").connect("accepted", "bounded", branch="false")
    graph.connect("bounded", "restore_answer", branch="true").connect("bounded", "reflect", branch="false")
    graph.connect("reflect", "save_experience").connect("save_experience", "next_attempt").connect("next_attempt", "model")
    graph.connect("restore_answer", "deliver")
    model_stage_routes(graph, ("evaluate", "reflect"))
    return graph.build(source={"template": "reflexion", "version": 1}, metadata={"preview_stages": ["prepare", "model", "tools", "evaluate", "reflect", "deliver"]})


def templates():
    return {"react": react(), "plan_execute": plan_execute(), "reflexion": reflexion()}
