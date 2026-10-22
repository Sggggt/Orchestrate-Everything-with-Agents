"""Bounded facts for an external planner; inspection never reserves or executes."""

import json
import statistics

from pydantic import ValidationError

from .errors import RuntimeFault, require
from .execution_policy import PLATFORM_TOOLS, request_hash
from .llm import model_fingerprint, model_required, model_setup
from .models import BudgetPolicy, TaskSpec, TERMINAL, content_hash, json_dump, now_ms
from .store import one, rows


class Admission:
    def __init__(self, runtime):
        self.rt = runtime

    def configuration(self, agent_id):
        config = self.rt.agent_configs.get(agent_id)
        require(config is not None, "NOT_FOUND", "Choose a managed Agent")
        return config

    def capacity(self, config, required_tools=()):
        adapter = self.rt.adapters.get(config.id)
        limit = min(config.max_concurrency, adapter.describe().max_concurrency) if adapter else 0
        result = []
        limits = [("execution:global", self.rt.config.max_concurrency - self.rt.config.reserved_external_slots),
                  ("execution:agent:" + config.id, limit)]
        if model_required(config):
            key = "model:" + content_hash({"base_url": config.base_url, "model": config.model})[:24]
            capacity = self.rt.resources.capacities.get(key)
            limits.append((key, capacity.limit - capacity.reserved if capacity else self.rt.config.max_concurrency))
        for sid in sorted({name.split(":", 2)[1] for name in required_tools if name.startswith("mcp:")}):
            if sid in self.rt.mcp.configs:
                limits.append(("mcp:service:" + sid, self.rt.mcp.capacity.policy(sid).max_service_calls))
        for key, maximum in limits:
            used = sum(self.rt.resources.quantities.get(key, {}).values())
            result.append({"key": key, "limit": maximum, "used": used, "available": max(0, maximum - used)})
        return result

    def facts(self, config):
        adapter = self.rt.adapters.get(config.id)
        descriptor = adapter.describe().model_dump(mode="json") if adapter else None
        native = set(PLATFORM_TOOLS)
        if config.kind == "llm":
            native -= {"put_file", "put_bytes"}
        if not config.collaboration:
            native -= {"list_threads", "send_message", "submit_task", "spawn_instances"}
        native.update(config.workbench.tools if config.workbench else ())
        bindings = []
        for binding in config.mcp_bindings:
            server = self.rt.mcp.configs.get(binding.server_id)
            catalog = self.rt.mcp.catalogs.get(binding.server_id, {})
            bindings.append({"server_id": binding.server_id, "tools": binding.tools,
                             "enabled": bool(server and server.enabled), "catalog": "cached" if catalog.get("discovered_at") else "unknown",
                             "discovered_at": catalog.get("discovered_at"), "protocol_version": catalog.get("protocolVersion")})
        skills = [{"id": s.id, "name": s.name, "enabled": s.enabled} for s in config.skills]
        for binding in config.skill_bindings:
            entry = self.rt.skills.entries.get(binding.skill_id)
            skills.append({"id": binding.skill_id, "enabled": bool(entry and entry["enabled"] and entry["skill"].enabled)})
        return {"agent_id": config.id, "name": config.name, "project_id": config.project_id,
                "enabled": config.enabled, "descriptor": descriptor,
                "purpose": {"source": "configured_agent_md", "excerpt": self.rt.vault.redact(config.agent_md[:640]), "verified": False},
                "input_contract": "TaskSpec.payload object; adapter-specific payload semantics are not declared",
                "delivery_contract": "TaskResult plus task result_policy; semantic quality requires review",
                "model": {**model_setup(config, has_api_key=bool(self.rt.vault.get(config.id))),
                          "reasoning_effort": config.reasoning_effort,
                          "name": config.model, "fingerprint": model_fingerprint(config), "credentials_in_fingerprint": False},
                "grants": {"native_tools": sorted(native), "skills": skills, "mcp_bindings": bindings},
                "capacity": self.capacity(config)}

    async def describe(self, agent_id):
        config = self.configuration(agent_id)
        from .services import resolve_project
        def read(db):
            resolve_project(db, config.project_id)
            return rows(db, "SELECT state,duration_ms,input_tokens,output_tokens,tools_requested,tool_calls,created_at "
                        "FROM model_observations WHERE agent_id=? AND fingerprint=? ORDER BY created_at DESC LIMIT 32",
                        (agent_id, model_fingerprint(config)))
        samples = await self.rt.store.call(read)
        facts = self.facts(config)
        successes = [s for s in samples if s.state == "SUCCEEDED"]
        measured = [s for s in samples if s.input_tokens is not None and s.output_tokens is not None]
        facts["observations"] = {"scope": "latest 32 model requests with current settings; mixed tasks, including summaries",
            "sample_count": len(samples), "successful_requests": len(successes),
            "first_at": min((s.created_at for s in samples), default=None), "last_at": max((s.created_at for s in samples), default=None),
            "successful_request_duration_ms": {"median": statistics.median([s.duration_ms for s in successes]) if successes else None,
                            "range": [min(s.duration_ms for s in successes), max(s.duration_ms for s in successes)] if successes else None},
            "reported_token_samples": len(measured), "unreported_usage_samples": len(samples) - len(measured),
            "input_tokens": sum(s.input_tokens for s in measured),
            "output_tokens": sum(s.output_tokens for s in measured), "tool_call_responses": sum(bool(s.tool_calls) for s in successes),
            "monetary_cost": None, "ranking": "unavailable: no matched workload or price observations",
            "semantic_quality": "not_reviewed"}
        facts["model"]["verification"] = "observed_response" if successes else "not_observed_with_current_settings"
        return facts

    def requirements(self, config, required_tools, required_skills, require_model, capabilities, tool_policy=None):
        blocks, unknown = [], []
        facts = self.facts(config)
        if not config.enabled or not facts["descriptor"]:
            blocks.append({"code": "AGENT_UNAVAILABLE"})
        elif facts["descriptor"]["execution_mode"] == "external":
            blocks.append({"code": "UNSUPPORTED_CAPABILITY", "capability": "managed_execution"})
        for name in capabilities:
            if not facts["descriptor"] or not facts["descriptor"][name]:
                blocks.append({"code": "UNSUPPORTED_CAPABILITY", "capability": name})
        if require_model or model_required(config):
            if not model_required(config) or not config.base_url or not config.model:
                blocks.append({"code": "AGENT_NOT_CONFIGURED", "capability": "model"})
            else:
                unknown.append({"code": "MODEL_PROVIDER_NOT_PROBED", "fingerprint": facts["model"]["fingerprint"]})
        skills = {s["id"] for s in facts["grants"]["skills"] if s["enabled"]}
        blocks.extend({"code": "SKILL_UNAVAILABLE", "skill_id": name} for name in required_skills if name not in skills)
        bindings = {b.server_id: b for b in config.mcp_bindings}
        for name in required_tools:
            if not name.startswith("mcp:"):
                if name not in facts["grants"]["native_tools"]:
                    blocks.append({"code": "PERMISSION_DENIED", "tool": name})
                continue
            parts = name.split(":", 2)
            if len(parts) != 3 or not parts[2]:
                blocks.append({"code": "INVALID_REQUEST", "tool": name})
                continue
            _, sid, tool = parts
            binding, server = bindings.get(sid), self.rt.mcp.configs.get(sid)
            if not binding or not server or not server.enabled or (binding.tools is not None and tool not in binding.tools) or (server.allowed_tools is not None and tool not in server.allowed_tools):
                blocks.append({"code": "PERMISSION_DENIED", "tool": name})
            else:
                catalog = self.rt.mcp.catalogs.get(sid, {})
                for initial in self.rt.mcp.capacity.policy(sid).initialization:
                    identifier = f"mcp:{sid}:{initial.tool_name}"
                    if ((binding.tools is not None and initial.tool_name not in binding.tools)
                            or (server.allowed_tools is not None and initial.tool_name not in server.allowed_tools)
                            or (tool_policy is not None and not tool_policy.permits(identifier))
                            or (catalog.get("discovered_at") and not any(t["name"] == initial.tool_name for t in catalog.get("tools", [])))):
                        blocks.append({"code": "MCP_INITIALIZATION_UNAVAILABLE", "tool": identifier})
                if not catalog.get("discovered_at"):
                    unknown.append({"code": "MCP_CATALOG_UNKNOWN", "tool": name})
                elif not any(t["name"] == tool for t in catalog.get("tools", [])):
                    blocks.append({"code": "MCP_TOOL_UNAVAILABLE", "tool": name})
                else:
                    unknown.append({"code": "MCP_LIVE_CONNECTION_NOT_PROBED", "tool": name})
        if config.workbench:
            try:
                self.rt.environments.validate(config)
            except RuntimeFault as exc:
                blocks.append(exc.as_dict())
        return blocks, unknown

    async def candidates(self, project_id, required_tools=(), required_skills=(), require_model=False, capabilities=(), cursor=None, limit=20):
        require(len(required_tools) <= 96 and len(required_skills) <= 32, "INVALID_REQUEST", "Too many requirements")
        require(set(capabilities) <= {"supports_suspend", "supports_fork", "supports_probe"}, "INVALID_REQUEST", "Select declared boolean execution capabilities")
        require(type(require_model) is bool, "INVALID_REQUEST", "require_model must be boolean")
        from .services import resolve_project
        await self.rt.store.call(lambda db: resolve_project(db, project_id))
        limit = max(1, min(int(limit), 50))
        selected = sorted((c for c in self.rt.agent_configs.values() if c.project_id == project_id and (not cursor or c.id > cursor)), key=lambda c: c.id)[:limit + 1]
        items = []
        for config in selected[:limit]:
            blocks, unknown = self.requirements(config, required_tools, required_skills, require_model, capabilities)
            items.append({"agent_id": config.id, "name": config.name, "eligible": not blocks,
                          "blockers": blocks, "unknown": unknown, "capacity": self.capacity(config, required_tools)})
        return {"items": items, "next_cursor": selected[limit - 1].id if len(selected) > limit else None,
                "ordering": "agent_id; cost/speed ranking unavailable", "reservation": False, "selection": "caller"}

    async def check(self, spec: TaskSpec | dict, budget_id=None):
        report = {"admissible": False, "reservation": False, "blockers": [], "unknown": [], "queue_reasons": [],
                  "checks": {"task_schema": "valid", "model_input": "unknown until context projection", "upstream_tool_schema": "not_probed"}}
        try:
            spec = TaskSpec.model_validate(spec)
            self.rt.clean_json(spec.model_dump(mode="json"))
        except ValidationError as exc:
            report["checks"]["task_schema"] = "invalid"
            report["blockers"] = [{"code": "INVALID_REQUEST", "fields": [list(e["loc"]) for e in exc.errors()]}]
            return report
        except RuntimeFault as exc:
            report["blockers"] = [exc.as_dict()]
            return report
        def inspect(db):
            prior = one(db, "SELECT id,state,request_hash,retry_of_task_id FROM tasks WHERE session_id=? AND idempotency_key=?", (spec.session_id, spec.idempotency_key))
            if prior:
                digest = request_hash(spec)
                if budget_id is not None:
                    digest = content_hash({"request_hash": digest, "budget_id": budget_id})
                require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Existing key has different input")
                require(prior.retry_of_task_id is None, "IDEMPOTENCY_CONFLICT", "Existing key belongs to a retry request")
                return {"existing_task": {"id": prior.id, "state": prior.state}}, None
            self.rt.tasks.validate_submission(db, spec)
            thread = one(db, "SELECT active_run_id,owner_task_id FROM threads WHERE id=?", (spec.thread_id,))
            waiting = [{"reason": "THREAD"}] if thread.active_run_id or thread.owner_task_id else []
            for identity in spec.depends_on:
                state = one(db, "SELECT state FROM tasks WHERE id=?", (identity,)).state
                if state not in TERMINAL:
                    waiting.append({"reason": "DEPENDENCY", "task_id": identity, "state": state})
                elif state != "SUCCEEDED" and spec.dependency_policy == "ALL_SUCCEEDED":
                    report["blockers"].append({"code": "DEPENDENCY_FAILED", "task_id": identity})
            budget = one(db, "SELECT * FROM execution_budgets WHERE id=?", (budget_id,)) if budget_id is not None else None
            require(budget_id is None or budget and budget.session_id == spec.session_id, "BUDGET_SCOPE_MISMATCH", "Budget is unavailable in this session")
            return {"queue_reasons": waiting}, budget
        try:
            shared, budget = await self.rt.store.call(inspect)
            report.update(shared)
            if "existing_task" in shared:
                report["admissible"] = True
                report["checks"]["model_input"] = "not_applicable: existing receipt"
                return report
            config = self.configuration(spec.agent_id)
            blocks, unknown = self.requirements(config, spec.tool_policy.required, spec.skill_policy.required, False, (), spec.tool_policy)
            report["blockers"].extend(blocks)
            report["unknown"].extend(unknown)
            for item in self.capacity(config, spec.tool_policy.required):
                if not item["available"]:
                    report["queue_reasons"].append({"reason": "CAPACITY", **item})
            report["queue_reasons"].extend(self.rt.resources.reasons("", spec.resources))
            if spec.deadline_at_ms is not None and spec.deadline_at_ms <= now_ms():
                report["blockers"].append({"code": "DEADLINE_EXCEEDED"})
            if budget:
                policy = json.loads(budget.policy_json)
                remaining = {name: max(0, policy["max_" + name] - budget[name]) for name in ("model_calls", "input_tokens", "output_tokens", "tool_calls")}
                report["budget"] = {"id": budget.id, "remaining": remaining}
                needed = ("model_calls", "input_tokens", "output_tokens") if model_required(config) else ()
                if any(remaining[n] == 0 for n in needed) or (budget.started_at and now_ms() >= budget.started_at + policy["max_elapsed_seconds"] * 1000):
                    report["blockers"].append({"code": "BUDGET_EXHAUSTED"})
                if (spec.tool_policy.required or spec.skill_policy.required or spec.tool_policy.mode == "required" or spec.skill_policy.mode == "required") and not remaining["tool_calls"]:
                    report["blockers"].append({"code": "BUDGET_EXHAUSTED", "limit": "tool_calls"})
            else:
                report["budget"] = {"source": "default_on_submission", "policy": BudgetPolicy().model_dump(mode="json")}
            report["checks"].update(request_bytes=len(json_dump(spec).encode()), request_limit_bytes=self.rt.config.max_message_bytes,
                                    subprocess_frame="not_built", task_payload_semantics="adapter_defined")
        except RuntimeFault as exc:
            report["blockers"].append(exc.as_dict())
        report["admissible"] = not report["blockers"]
        report["ready_now"] = report["admissible"] and not report["queue_reasons"] and not report["unknown"]
        report["dependencies"] = {"tools": spec.tool_policy.required, "skills": spec.skill_policy.required, "tasks": spec.depends_on}
        return report
