"""Public engine introspection; shutdown is a private transport command."""
from ..registry import method


@method("engine.status")
def status(engine, params):
    return {"state": "busy" if engine.progress.running else "ready", "engineVersion": engine.version,
            "contractVersion": 1, "collectionOpen": engine.col.db is not None,
            "mediaPrefix": None, "message": None}


@method("engine.describe")
def describe(engine, params):
    return engine.registry.describe()
