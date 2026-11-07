"""Apply the existing transport recovery contract to the declarative ReAct graph."""

import test_context_recovery as recovery
from oea import AgentArchitecture, ArchitectureBinding
from oea.orchestration.architecture_templates import react


class DeclarativeRecoveryTests(recovery.RecoveryTests):
    async def configure(self, calls=128):
        await super().configure(calls)
        await self.host.architectures.create(AgentArchitecture(id="recovery", project_id="p", name="Recovery"), react().model_dump(mode="json"))
        await self.host.architectures.publish("recovery", "p", 1)
        saved = await self.host.agents.get("worker")
        await self.host.architectures.bind("worker", "p", ArchitectureBinding(architecture_id="recovery", version=1), saved["revision"])
