"""Native hierarchical tag operations with exact-selection safeguards."""
import re

from ..collection import mutation_result, undo_state
from ..errors import EngineError
from ..registry import method
from .decks import _result
from .notes import _tag_equal, _tag_name, _tags, _text


def _tree(col, progress=None):
    result = []
    pending = [(node, None) for node in reversed(col.tags.tree().children)]
    while pending:
        if progress is not None and len(result) % 512 == 0:
            progress.check_abort()
            progress.report(len(result), None, "Reading tag tree")
        node, parent = pending.pop()
        name = parent + "::" + node.name if parent else node.name
        if len(name) > 1024 or node.level > 101 or len(result) >= 100000:
            raise EngineError("unsupported", "The tag tree exceeds the supported response size.")
        result.append({"name": name, "parent": parent, "level": node.level - 1,
                       "collapsed": node.collapsed, "noteCount": 0})
        pending.extend((child, name) for child in reversed(node.children))
    return result


def _existing(col, names, progress=None):
    if not names:
        raise EngineError("invalid_params", "Select at least one tag.")
    known = {node["name"] for node in _tree(col, progress)}
    candidates = {}
    for name in known:
        candidates.setdefault(name.casefold(), []).append(name)
    result = []
    for name in _tags(names):
        if progress is not None:
            progress.check_abort()
        canonical = name if name in known else next((item for item in candidates.get(name.casefold(), [])
                                                    if _tag_equal(col, item, name)), None)
        if canonical is None:
            raise EngineError("not_found", "The requested tag is unavailable.")
        if canonical not in result:
            result.append(canonical)
    return result


def _under(col, name, root):
    parts, prefix = name.split("::"), root.split("::")
    return len(parts) >= len(prefix) and all(_tag_equal(col, left, right) for left, right in zip(parts, prefix))


def _check_destinations(col, replacements, progress):
    tree = _tree(col, progress)
    for index, node in enumerate(tree):
        progress.check_abort()
        if index % 512 == 0:
            progress.report(index, len(tree), "Checking tag destinations")
        name = node["name"]
        for old, new in replacements:
            progress.check_abort()
            if _under(col, name, old):
                name = new + name[len(old):]
        if len(name) > 1024 or name.count("::") > 100:
            raise EngineError("invalid_params", "The resulting tag hierarchy exceeds the supported size.")


@method("tags.list")
def list_tags(engine, params):
    col = engine.col
    progress = engine.progress
    with progress.using_backend(None):
        prefix = _text(params.get("prefix", ""))
        tags = _tree(col, progress)
        counts = {node["name"]: 0 for node in tags}
        candidates = {}
        for name in counts:
            candidates.setdefault(name.casefold(), []).append(name)
        last_id, processed = None, 0
        progress.report(0, None, "Counting tag notes")
        while True:
            progress.check_abort()
            rows = (col.db.all("select id,tags from notes order by id limit 512") if last_id is None else
                    col.db.all("select id,tags from notes where id > ? order by id limit 512", last_id))
            if not rows:
                break
            for _note_id, joined in rows:
                progress.check_abort()
                ancestors = set()
                for name in col.tags.split(joined):
                    progress.check_abort()
                    parts = name.split("::")
                    for index in range(1, len(parts) + 1):
                        ancestor = "::".join(parts[:index])
                        canonical = ancestor if ancestor in counts else next(
                            (key for key in candidates.get(ancestor.casefold(), [])
                             if _tag_equal(col, key, ancestor)), None)
                        if canonical is not None:
                            ancestors.add(canonical)
                for name in ancestors:
                    counts[name] += 1
            processed += len(rows)
            last_id = rows[-1][0]
            progress.report(processed, None, "Counting tag notes")
        result = []
        for index, node in enumerate(tags):
            if index % 512 == 0:
                progress.check_abort()
            node["noteCount"] = counts[node["name"]]
            if not prefix or _tag_equal(col, node["name"][:len(prefix)], prefix):
                result.append(node)
        progress.check_abort()
        return {**mutation_result(undo_state(col)), "tags": result}


@method("tags.rename")
def rename(engine, params):
    col = engine.col
    progress = engine.progress
    with progress.using_backend(None):
        old = _existing(col, [params["name"]], progress)[0]
        new = _tag_name(params["newName"])
        if params["includeChildren"]:
            _check_destinations(col, [(old, new)], progress)
            commit, arguments = col.tags.rename, (old, new)
        else:
            has_children = False
            for node in _tree(col, progress):
                progress.check_abort()
                if node["name"] != old and _under(col, node["name"], old):
                    has_children = True
                    break
            if has_children:
                # A parent still represents its surviving children. Exact native
                # replacement preserves their names and collapse state.
                commit = col.tags.find_and_replace
                arguments = ([], "^" + re.escape(old) + "$", new.replace("$", "$$"), True, False)
            else:
                commit, arguments = col.tags.rename, (old, new)
        progress.report(0, 1, "Renaming tags")
        progress.check_abort()
    result = commit(*arguments)
    return _result(col, result.changes, count=result.count)


@method("tags.reparent")
def reparent(engine, params):
    col = engine.col
    progress = engine.progress
    with progress.using_backend(None):
        names = _existing(col, params["names"], progress)
        parent = _tag_name(params["parent"], allow_empty=True)
        if parent:
            for name in names:
                progress.check_abort()
                if _under(col, parent, name):
                    raise EngineError("invalid_params", "A tag cannot be moved into its own subtree.")
        replacements = [(name, (parent + "::" if parent else "") + name.split("::")[-1]) for name in names]
        _check_destinations(col, replacements, progress)
        progress.report(0, 1, "Reparenting tags")
        progress.check_abort()
    result = col.tags.reparent(names, parent)
    return _result(col, result.changes, count=result.count)


@method("tags.remove")
def remove(engine, params):
    col = engine.col
    progress = engine.progress
    with progress.using_backend(None):
        names = _existing(col, params["names"], progress)
        commit, arguments = col.tags.remove, (" ".join(names),)
        if not params["includeChildren"]:
            selected, blocked = set(names), set()
            # In reverse tree order, an unselected descendant blocks pruning
            # its ancestors. The native tree already supplies canonical names.
            for node in reversed(_tree(col, progress)):
                progress.check_abort()
                if node["name"] not in selected or node["name"] in blocked:
                    if node["parent"] is not None:
                        blocked.add(node["parent"])
            clearable = [name for name in names if name not in blocked]
            if clearable and len(clearable) != len(names):
                raise EngineError("unsupported", "First call tags.remove with includeChildren: false for only parents "
                                  "whose descendants must remain, then make a separate tags.remove request with "
                                  "includeChildren: false for only leaf tags or fully selected subtrees. "
                                  "The two requests are not jointly atomic.")
            if not clearable:
                pattern = "^(?:" + "|".join(re.escape(name) for name in names) + ")$"
                commit, arguments = col.tags.find_and_replace, ([], pattern, "", True, False)
        progress.report(0, 1, "Removing tags")
        progress.check_abort()
    result = commit(*arguments)
    return _result(col, result.changes, count=result.count)


@method("tags.clearUnused")
def clear_unused(engine, _params):
    with engine.progress.using_backend(None):
        engine.progress.report(0, 1, "Clearing unused tags")
        engine.progress.check_abort()
    result = engine.col.tags.clear_unused_tags()
    return _result(engine.col, result.changes, count=result.count)


@method("tags.setCollapsed")
def set_collapsed(engine, params):
    name = _existing(engine.col, [params["name"]])[0]
    # Anki persists expansion without adding an undo step; report its real status.
    return _result(engine.col, engine.col.tags.set_collapsed(name, params["collapsed"]))
