"""Offline-only decoders for retired declarations; never used by the Host."""

from pydantic import Field

from oea.contracts.configuration import AgentDefinition as CurrentDefinition, Manager, Model

PROTOCOL_VERSION = "3.0"


class LegacyManager(Manager):
    model: Model = Field(default_factory=Model)
    summary_model: Model | None = None


class AgentDefinition(CurrentDefinition):
    model: Model = Field(default_factory=Model)
    summary_model: Model | None = None
    manager: LegacyManager | None = None


def manager_snapshot(definition):
    from oea.contracts.configuration import manager_snapshot as select
    selected = select(definition)
    return selected.model_copy(update={"model": definition.manager.model, "summary_model": definition.manager.summary_model})
