"""Explicit process-local code graphs with persistent OEA checkpoints."""

from oea.contracts.errors import require


class AgentGraphs:
    def __init__(self, host):
        self.host, self.factories, self.compiled = host, {}, {}

    def register(self, name, factory, *, version):
        from oea.contracts.configuration import Execution
        value = Execution(graph=name, graph_version=version)
        key = value.graph, value.graph_version
        require(callable(factory) and key not in self.factories, "GRAPH_CONFLICT", "Register one graph factory for each name and version")
        self.factories[key] = factory

    def available(self, execution):
        return bool(execution.program) or (execution.graph, execution.graph_version) in self.factories

    def resolve(self, execution):
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

    async def list(self):
        values = []
        for name, version in self.factories:
            from oea.contracts.configuration import Execution
            graph = self.resolve(Execution(graph=name, graph_version=version))
            topology = graph.get_graph()
            values.append({"name": name, "version": version, "editable": False, "nodes": list(topology.nodes),
                           "edges": [{"source": edge.source, "target": edge.target, "conditional": edge.conditional} for edge in topology.edges]})
        return values
