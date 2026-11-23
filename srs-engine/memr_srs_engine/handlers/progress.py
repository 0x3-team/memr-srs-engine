"""The only implemented collection-operation controls in wave zero."""
from ..registry import method


@method("progress.get")
def get(engine, params):
    return engine.progress.get(params.get("operationId"))


@method("progress.abort")
def abort(engine, params):
    return engine.progress.abort(params["operationId"])
