"""Restricted run capabilities; delegation reuses the ordinary domain services."""

from oea.catalog.service import need
from oea.contracts.domain import MessageSpec, RunHandle, RunSpec, Wait
from oea.contracts.errors import RuntimeFault, require
from oea.collaboration.messages import Messages
from oea.collaboration.boards import Boards
from oea.storage.persistence import one, rows, now_ms
from oea.tools.contracts import ToolSpec
from oea.workspace.service import schema


STRING = {"type": "string", "minLength": 1}
RUNS = {"type": "array", "items": STRING, "minItems": 1, "maxItems": 128, "uniqueItems": True}
SCHEMAS = {
    "agent_cards": schema({"agent_ids": {**RUNS, "maxItems": 64}}, ["agent_ids"]),
    "list_instances": schema({"agent_id": STRING, "cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    "create_instance": schema({"name": {**STRING, "maxLength": 256}}, ["name"]),
    "assign_work": schema({"instance_id": STRING, "input": {"oneOf": [{"type": "string"}, {"type": "object"}]}, "artifact_ids": {"type": "array", "items": STRING, "maxItems": 32, "uniqueItems": True}, "idempotency_key": {**STRING, "maxLength": 128}}, ["instance_id", "input", "idempotency_key"]),
    "inspect_work": schema({"run_id": STRING}, ["run_id"]),
    "wait_for_runs": schema({"run_ids": RUNS, "policy": {"enum": ["all_succeeded", "all_settled"]}}, ["run_ids"]),
    "list_members": schema({"cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    "list_boards": schema({"cursor": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    "read_messages": schema({"board_id": STRING, "after": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["board_id"]),
    "send_message": schema({"board_id": STRING, "mentions": {"type": "array", "items": STRING, "maxItems": 256}, "content": {**STRING, "maxLength": 65536},
        "reply_to": STRING, "topic_id": STRING, "context_refs": {"type": "array", "items": STRING, "maxItems": 128},
        "artifact_ids": {"type": "array", "items": STRING, "maxItems": 32},
        "idempotency_key": {**STRING, "maxLength": 128}}, ["board_id", "content", "idempotency_key"]),
    "request_cancel": schema({"run_id": STRING}, ["run_id"]),
    "report_issue": schema({"content": {**STRING, "maxLength": 65536}, "idempotency_key": {**STRING, "maxLength": 128}}, ["content", "idempotency_key"]),
}
DESCRIPTIONS = {
    "agent_cards": "Read currently granted department capability cards without private instructions or credentials.",
    "list_instances": "List instances of this Agent or related work in a granted partner department. Use the returned cursor.",
    "create_instance": "Create a named instance of this Agent group, inheriting its saved business capabilities and stable workspace.",
    "assign_work": "Assign explicit work to an instance in this Agent group. Preserve the key when reading back an accepted assignment; all costs inherit this root intent.",
    "inspect_work": "Read related run state, actions, partial artifacts and complete delivery. Reading does not submit work.",
    "wait_for_runs": "Suspend this graph until the listed related work has actual terminal receipts. Unknown outcomes require explicit inspection.",
    "list_members": "List stable identities, names, reachable inboxes and communication scope. This does not create managers or other instances.",
    "list_boards": "List shared boards and private inboxes with current explicit read access.",
    "read_messages": "Read original board entries incrementally using a durable instance cursor. Explicit after can reread history. Reading never starts work.",
    "send_message": "Persist free-form text on a board. Optional mentions send notifications only to existing authorized readers. Check issues for undelivered mentions. New work requires assign_work.",
    "request_cancel": "Request cancellation of a related instance in this Agent group; returns actual stop observations.",
    "report_issue": "Record a problem or evidence for the controller without triggering another model discussion.",
}
COORDINATION_TOOLS = set(SCHEMAS)
DELEGATION_TOOLS = {"create_instance", "assign_work", "request_cancel"}
INSTANCE_TOOLS = COORDINATION_TOOLS - DELEGATION_TOOLS
WRITES = {"create_instance", "assign_work", "send_message", "request_cancel", "report_issue"}


class Coordination:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def actor(db, run_id):
        run = need(db, "runs", run_id)
        instance = need(db, "instances", run["instance_id"])
        session = need(db, "sessions", instance["session_id"])
        return Messages.source(db, run_id, session)

    @staticmethod
    def related(db, actor, run_id, *, cancel=False):
        source, instance, departments, _ = actor
        run = need(db, "runs", run_id)
        target = need(db, "instances", run["instance_id"])
        require(target["session_id"] == instance["session_id"] and target["agent_id"] in departments
                and (run["root_run_id"], run["root_workflow_run_id"]) == (source["root_run_id"], source["root_workflow_run_id"]),
                "PERMISSION_DENIED", "Work belongs to another authorized intent or department")
        if cancel:
            require(actor[3] and target["agent_id"] in departments, "PERMISSION_DENIED", "Cancellation requires a current work delegation grant")
            ancestor = one(db, "WITH RECURSIVE ancestors(id,parent_run_id) AS (SELECT id,parent_run_id FROM runs WHERE id=? UNION ALL SELECT r.id,r.parent_run_id FROM runs r JOIN ancestors a ON a.parent_run_id=r.id) SELECT 1 FROM ancestors WHERE id=?", (source["id"], run_id))
            require(ancestor is None, "PERMISSION_DENIED", "Request controller cancellation of an ancestor intent")
        return run

    def check(self, db, run_id, name, args):
        actor = self.actor(db, run_id)
        run, instance, departments, can_request = actor
        require(name in COORDINATION_TOOLS and (name not in DELEGATION_TOOLS or can_request), "PERMISSION_DENIED", "Work delegation is not currently granted")
        if name in {"inspect_work", "request_cancel"}:
            self.related(db, actor, args["run_id"], cancel=name == "request_cancel")
        elif name == "wait_for_runs":
            for identity in args["run_ids"]:
                require(identity != run_id, "INVALID_REQUEST", "A run cannot wait for itself")
                self.related(db, actor, identity)
        elif name == "agent_cards":
            require(set(args["agent_ids"]) <= departments, "PERMISSION_DENIED", "Card belongs to a department outside the current grant")
        elif name == "list_instances":
            require(args.get("agent_id", instance["agent_id"]) in departments, "PERMISSION_DENIED", "Department is not currently granted")
        elif name == "assign_work":
            target = need(db, "instances", args["instance_id"])
            require(target["session_id"] == instance["session_id"] and target["agent_id"] in departments and target["id"] != instance["id"], "PERMISSION_DENIED", "Assign work only to another reachable instance")
        elif name in {"send_message", "read_messages"}:
            Boards.check(db, args["board_id"], project_id=instance["project_id"], instance=instance,
                         write=name == "send_message", departments=departments)
        return actor

    async def execute(self, run_id, name, args, *, action_id):
        def perform(db):
            actor = self.check(db, run_id, name, args)
            run, instance, departments, _ = actor
            if name == "create_instance":
                value = self.host.instances.view(self.host.instances._create(db, instance["session_id"], instance["agent_id"], name=args["name"]))
                return self.host.tools.commit_owned(db, action_id, run_id, value)
            if name == "assign_work":
                child, reused = self.host.runs._submit(db, RunSpec(instance_id=args["instance_id"], input=args["input"], artifact_ids=args.get("artifact_ids", ()), idempotency_key="assignment:" + args["idempotency_key"], parent_run_id=run_id))
                value = RunHandle(id=child["id"], instance_id=child["instance_id"], root_run_id=child["root_run_id"], root_workflow_run_id=child["root_workflow_run_id"], budget_id=child["budget_id"], state=child["state"], reused=reused).model_dump(mode="json")
                return self.host.tools.commit_owned(db, action_id, run_id, value)
            if name == "agent_cards":
                cards = []
                for identity in args["agent_ids"]:
                    value = self.host.definitions.get(identity)
                    require(value is not None and value.project_id in {None, instance["project_id"]}, "PERMISSION_DENIED", "Card belongs to another project")
                    cards.append({"agent_id": identity, "name": value.name, "description": value.description, "enabled": value.enabled,
                        "tools": [tool for tool in (*value.tools.local, *value.tools.functions) if tool in self.host.tools.providers
                                  and (tool != "exec_command" or self.host.tools.providers[tool].commands_enabled)],
                        "skills": [{"id": skill.id, "required": skill.required} for skill in value.skills], "manager_configured": value.manager is not None})
                return {"cards": cards}
            if name == "list_members":
                limit, cursor = args.get("limit", 25), args.get("cursor", 0)
                values = rows(db, "SELECT i.rowid AS position,i.id,i.name,i.agent_id,i.role,b.id AS inbox_board_id FROM instances i JOIN agent_definitions a ON a.id=i.agent_id LEFT JOIN boards b ON b.owner_instance_id=i.id AND b.scope='inbox' WHERE i.session_id=? AND i.agent_id IN (" + ",".join("?" for _ in departments) + ") AND json_extract(a.config_json,'$.enabled')=1 AND i.rowid>? ORDER BY i.rowid LIMIT ?", (instance["session_id"], *departments, cursor, limit + 1))
                return {"items": [value | {"can_notify": True, "can_assign": actor[3] and value["id"] != instance["id"], "inbox_readable": bool(one(db, "SELECT 1 FROM board_readers WHERE board_id=? AND instance_id=?", (value["inbox_board_id"], instance["id"])))} for value in values[:limit]],
                        "cursor": values[limit - 1]["position"] if len(values) > limit else None}
            if name == "list_boards":
                return Boards.page(db, instance["project_id"], instance["session_id"], instance, args.get("cursor", 0), args.get("limit", 25))
            if name == "read_messages":
                saved = one(db, "SELECT position FROM board_read_positions WHERE board_id=? AND consumer_id=?", (args["board_id"], instance["id"]))
                value = self.host.messages.page(db, args["board_id"], project_id=instance["project_id"], instance=instance,
                    after=args.get("after", saved["position"] if saved else 0), limit=args.get("limit", 25))
                self.host.messages.save_position(db, args["board_id"], instance["id"], value["next_position"])
                for item in value["items"]:
                    db.execute("UPDATE deliveries SET read_at=COALESCE(read_at,?) WHERE message_id=? AND instance_id=?", (now_ms(), item["id"], instance["id"]))
                return self.host.tools.commit_owned(db, action_id, run_id, value)
            if name == "list_instances":
                agent = args.get("agent_id", instance["agent_id"])
                limit, cursor = args.get("limit", 25), args.get("cursor", 0)
                query = "SELECT i.rowid AS position,i.* FROM instances i WHERE i.session_id=? AND i.agent_id=? AND i.rowid>?"
                params = [instance["session_id"], agent, cursor]
                if agent != instance["agent_id"]:
                    query += " AND EXISTS(SELECT 1 FROM runs r WHERE r.instance_id=i.id AND r.root_run_id IS ? AND r.root_workflow_run_id IS ?)"
                    params.extend((run["root_run_id"], run["root_workflow_run_id"]))
                values = rows(db, query + " ORDER BY i.rowid LIMIT ?", (*params, limit + 1))
                return {"items": [{**self.host.instances.view(value), "position": value["position"]} for value in values[:limit]],
                    "cursor": values[limit - 1]["position"] if len(values) > limit else None}
            return actor
        try:
            value = await self.host.store.call(perform, transaction=name in {"create_instance", "assign_work", "read_messages"})
        except RuntimeFault as error:
            # Scope denial or validation in this phase precedes any external
            # action, and local mutations have rolled back in the same tx.
            return {"isError": True, "error": error.as_dict()}
        if name == "assign_work":
            self.host.launch(value["id"])
            self.host.notify()
        if name in {"create_instance", "assign_work", "agent_cards", "list_instances", "list_members", "list_boards", "read_messages"}:
            return value
        run, instance, _, _ = value
        if name == "inspect_work":
            receipt = await self.host.runs.inspect(args["run_id"])
            if receipt["state"] == "completed":
                receipt["result"] = (await self.host.runs.result(args["run_id"])).model_dump(mode="json")
            return receipt
        if name == "request_cancel":
            return await self.host.runs.cancel(args["run_id"])
        if name == "wait_for_runs":
            return {"wait": Wait(kind="runs", run_ids=tuple(args["run_ids"]), policy=args.get("policy", "all_settled")).model_dump(mode="json")}
        if name == "report_issue":
            target = await self.host.store.call(lambda db: one(db, "SELECT id FROM boards WHERE session_id=? AND scope='session' ORDER BY rowid LIMIT 1", (instance["session_id"],)))
            args = {"board_id": target["id"], **args}
        try:
            return await self.host.messages.send_from(run_id, MessageSpec(**args), action_id=action_id)
        except RuntimeFault as error:
            # The message and its tool result commit together. A RuntimeFault
            # from acceptance cannot leave a half-accepted message.
            return {"isError": True, "error": error.as_dict()}


class CoordinationTool:
    def __init__(self, host, name):
        self.host, self.name = host, name
        self.spec = ToolSpec(name=name, description=DESCRIPTIONS[name], input_schema=SCHEMAS[name],
            side_effects="local" if name in WRITES else "none", cancellation="cooperative")

    def describe(self):
        return self.spec

    async def authorize_replay(self, run_id, args):
        await self.host.store.call(lambda db: self.host.coordination.check(db, run_id, self.name, args))

    async def invoke(self, request):
        return await self.host.coordination.execute(request["run_id"], self.name, request["arguments"], action_id=request["action_id"])
