"""Register a model connection for an isolated test Host."""

from uuid import uuid4


async def register_model(host, model):
    saved = await host.model_profiles.save(f"{model.model or 'model'}-{uuid4().hex}", model)
    return saved["id"]
