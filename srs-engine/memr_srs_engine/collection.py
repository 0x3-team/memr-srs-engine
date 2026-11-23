"""The engine owns exactly one collection until all work has joined."""
from pathlib import Path
from anki.collection import Collection


class CollectionOwner:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.col = None

    def open(self):
        if self.col is not None:
            raise RuntimeError("Collection is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Anki holds the collection/media lock across its lifetime. Do not add a
        # sidecar lock that could disagree with Anki or survive a process crash.
        self.col = Collection(str(self.path))
        return self.col

    def close(self):
        if self.col is not None:
            self.col.close()
            self.col = None


def mutation_result(undo=None):
    return {
        "changes": {key: False for key in (
            "card", "note", "deck", "tag", "notetype", "config", "deckConfig",
            "mtime", "browserTable", "noteText", "studyQueues")},
        "undo": dict(undo or {"canUndo": False, "undoLabel": None, "canRedo": False, "redoLabel": None}),
    }


def undo_state(col):
    status = col.undo_status()
    return {"canUndo": bool(status.undo), "undoLabel": status.undo or None,
            "canRedo": bool(status.redo), "redoLabel": status.redo or None}
