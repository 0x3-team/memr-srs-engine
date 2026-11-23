"""Handlers accept (engine, params); metadata comes only from the catalogue.

Short handlers return their result schema. Long handlers return the terminal
progress.get result envelope (including changes and undo); the engine supplies
the initial operationId acknowledgement and manages polling and cancellation.
"""
import json
from pathlib import Path
import re

from .validate import check_schema

CATALOGUE = Path(__file__).resolve().parents[2] / "shared/srs/contract/catalogue"
HANDLERS = {}
METHOD_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9]*\.[a-zA-Z][a-zA-Z0-9]*\Z")


def method(name):
    def register(handler):
        if type(name) is not str or not METHOD_NAME.fullmatch(name) or name in HANDLERS:
            raise ValueError("Invalid or duplicate engine method registration")
        HANDLERS[name] = handler
        return handler
    return register


def load_catalogue(directory=CATALOGUE):
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    if index["version"] != 1:
        raise ValueError("Unsupported contract version")
    catalogue = {}
    for area in index["areas"]:
        if type(area) is not str or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]*\.json", area):
            raise ValueError("Invalid catalogue area path")
        document = json.loads((directory / area).read_text(encoding="utf-8"))
        if document["version"] != index["version"] or document["area"] != area[:-5]:
            raise ValueError("Catalogue area version or name disagrees")
        for entry in document["methods"]:
            name = entry["name"]
            if not METHOD_NAME.fullmatch(name) or not name.startswith(document["area"] + ".") or name in catalogue:
                raise ValueError("Invalid or duplicate catalogue method")
            if type(entry["mutating"]) is not bool or type(entry["longOperation"]) is not bool:
                raise ValueError("Invalid catalogue flags")
            check_schema(entry["params"])
            check_schema(entry["result"])
            catalogue[name] = entry
    return catalogue


class Registry:
    def __init__(self, catalogue=None, handlers=None):
        self.catalogue = load_catalogue() if catalogue is None else catalogue
        self.handlers = dict(HANDLERS if handlers is None else handlers)
        if set(self.handlers) - set(self.catalogue):
            raise ValueError("Engine handlers outside the catalogue")

    def describe(self):
        return {"version": 1, "methods": [
            {key: self.catalogue[name][key] for key in ("name", "description", "mutating", "longOperation")}
            for name in sorted(self.handlers)
        ]}
