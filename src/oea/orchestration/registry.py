"""Explicit process-local code graphs with persistent OEA checkpoints."""

from importlib.metadata import version as package_version

from oea.contracts.errors import require


class AgentGraphs:
    def __init__(self, host):
        self.host, self.factories, self.compiled, self.metadata = host, {}, {}, {}
        self.canvases = {}

    def canvas(self, name, version):
        """Cache the known topology of one registered graph, without execution."""
        from oea.contracts.configuration import Execution
        from oea.orchestration.architecture_preview import canvas_preview
        key = name, version
        if key not in self.canvases:
            topology = self.resolve(Execution(graph=name, graph_version=version)).get_graph()
            nodes = [{'id': identity, 'type': 'python', 'title': identity} for identity in topology.nodes]
            edges = [{'id': 'edge-' + str(index), 'source': edge.source, 'target': edge.target,
                      'branch': str(edge.data or 'default')} for index, edge in enumerate(topology.edges)]
            self.canvases[key] = {**canvas_preview({'nodes': nodes, 'edges': edges, 'entry': '__start__'}),
                                  'omitted': any(edge.conditional for edge in topology.edges)}
        return self.canvases[key]

    def register(self, name, factory, *, version, required_tools=(), required_skills=(), source=None):
        from oea.contracts.configuration import Execution
        value = Execution(graph=name, graph_version=version)
        key = value.graph, value.graph_version
        require(callable(factory) and key not in self.factories, "GRAPH_CONFLICT", "Register one graph factory for each name and version")
        self.factories[key] = factory
        self.metadata[key] = {"required_tools": tuple(required_tools), "required_skills": tuple(required_skills), "source": source or {}}

    def preview(self, name, version):
        from oea.contracts.configuration import Execution
        graph = self.resolve(Execution(graph=name, graph_version=version))
        topology = graph.get_graph()
        stages = [key for key in topology.nodes if key not in {"__start__", "__end__"}]
        return {"stages": [{"id": key, "title": key, "type": "python", "description": "Registered Python node"} for key in stages[:8]],
                "omitted": len(stages) > 8 or any(edge.conditional for edge in topology.edges),
                "description": "Known compiled topology; dynamic paths may be omitted", "editable": False,
                **self.metadata[(name, version)]}

    def available(self, execution):
        return bool(execution.program or execution.architecture) or (execution.graph, execution.graph_version) in self.factories

    def resolve(self, execution):
        require(execution.architecture is None, "ARCHITECTURE_RESOLUTION_REQUIRED", "Resolve published architectures in their project scope")
        if execution.program:
            from oea.orchestration.graphs.process import compile_process_graph
            key = ("_process", "2")
            if key not in self.compiled:
                self.compiled[key] = compile_process_graph(self.host.saver)
            return self.compiled[key]
        key = execution.graph, execution.graph_version
        require(key in self.factories and not execution.program, "EXECUTION_UNAVAILABLE", "Register the saved graph version in the host process")
        if key not in self.compiled:
            graph = self.factories[key](self.host.saver)
            require(getattr(graph, "checkpointer", None) is self.host.saver, "PERSISTENT_GRAPH_REQUIRED", "Compile code graphs with the supplied persistent checkpointer")
            self.compiled[key] = graph
        return self.compiled[key]

    async def resolve_execution(self, execution, project_id, *, allow_trusted=True):
        require(allow_trusted or not execution.program and execution.graph == "default", "PERMISSION_DENIED", "Custom execution requires explicit trusted extension authorization")
        if execution.architecture is None:
            return self.resolve(execution)
        import json
        from oea.contracts.configuration import Execution
        from oea.orchestration.graphs.architecture import compile_architecture
        binding = execution.architecture
        owner, saved = await self.host.store.call(lambda db: self.host.architectures._version(db, binding, project_id))
        if not allow_trusted:
            from oea.applications.execution import architecture_requires_trust
            require(not architecture_requires_trust(owner, json.loads(saved["definition_json"])),
                    "PERMISSION_DENIED", "Code extensions in an architecture require explicit application authorization")
        key = ("architecture", binding.architecture_id, binding.version, saved["sha256"])
        if key not in self.compiled:
            dependencies = json.loads(saved["dependencies_json"])
            changed = {name: {"published": expected, "installed": package_version(name)} for name, expected in dependencies.items() if package_version(name) != expected}
            require(not changed, "ARCHITECTURE_DEPENDENCY_MISMATCH", "Restore the published dependency versions before executing this architecture", details={"dependencies": changed})
            definition = json.loads(saved["definition_json"])
            if owner["representation"] == "python":
                self.compiled[key] = self.resolve(Execution(graph=definition["graph"], graph_version=definition["graph_version"]))
            else:
                self.compiled[key] = compile_architecture(definition, self.host.saver, functions=self.host.workflows.functions)
        return self.compiled[key]

    async def requirements(self, execution, project_id):
        key = execution.graph, execution.graph_version
        if execution.architecture:
            import json
            owner, saved = await self.host.store.call(lambda db: self.host.architectures._version(db, execution.architecture, project_id))
            if owner["representation"] != "python":
                return {}
            declaration = json.loads(saved["definition_json"])
            key = declaration["graph"], declaration["graph_version"]
        return self.metadata.get(key, {})

    async def list(self):
        values = []
        for name, version in self.factories:
            from oea.contracts.configuration import Execution
            graph = self.resolve(Execution(graph=name, graph_version=version))
            topology = graph.get_graph()
            values.append({"name": name, "version": version, "editable": False, "nodes": list(topology.nodes),
                           "edges": [{"source": edge.source, "target": edge.target, "conditional": edge.conditional} for edge in topology.edges], **self.metadata[(name, version)]})
        return values
