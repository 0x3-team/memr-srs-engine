"""Pinned native FSRS operations with cancellation and disposable cache writes."""
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time

from ..collection import Collection, mutation_result, undo_state
from anki import scheduler_pb2
from anki.config_pb2 import ConfigKey
from ..errors import EngineError, public_error
from ..progress import backend_progress
from ..registry import method
from .deck_options import REVIEW_ORDER, _normal_deck, _preset, _request
from .decks import _result, _text

MAX_SELECTED_CARDS = 100000
MAX_SIMULATED_CARD_DAYS = 50000000
MAX_COMPUTE_SECONDS = 120
MAX_WORKER_BYTES = 32 * 1024 * 1024
# Inference ranges from the pinned fsrs-rs 6.6.2 parameter_clipper.rs. Reject
# requests that native FSRS would silently clip; never return unapplied weights.
PARAMETER_BOUNDS = ((.001, 100),) * 4 + ((1, 10), (.001, 4), (.001, 4), (.001, .75),
    (0, 4.5), (0, .8), (.001, 3.5), (.001, 5), (.001, .25), (.001, .9), (0, 4),
    (0, 1), (1, 6), (0, 2), (0, 2), (0, .8), (.1, .8))


@method("fsrs.getPreferences")
def get_preferences(engine, params):
    return {"loadBalancerEnabled": engine.col.load_balancer_enabled,
            "shortTermWithStepsEnabled": engine.col.fsrs_short_term_with_steps_enabled}


@method("fsrs.setPreferences")
def set_preferences(engine, params):
    col = engine.col
    current = get_preferences(engine, {})
    if "expected" in params and params["expected"] != current:
        raise EngineError("conflict", "FSRS scheduling preferences changed. Reload them before saving.")
    desired = {key: params[key] for key in current}
    if current == desired:
        return mutation_result(undo_state(col))
    changed = None

    def apply():
        nonlocal changed
        step = col.add_custom_undo_entry("FSRS scheduling preferences")
        # Use the specialized setter so Anki also invalidates its load balancer.
        col._backend.set_load_balancer_enabled(params["loadBalancerEnabled"])
        col.set_config_bool(ConfigKey.Bool.FSRS_SHORT_TERM_WITH_STEPS_ENABLED,
                            params["shortTermWithStepsEnabled"], undoable=True)
        changed = col.merge_undo_entries(step)
    col.db.transact(apply)
    return _result(col, changed)


def _parameters(values):
    if len(values) not in (0, 17, 19, 21) or any(not math.isfinite(value) for value in values):
        raise EngineError("invalid_params", "FSRS parameters require 17, 19 or 21 finite values, or defaults.")
    # Protobuf and the native model store f32 values. Compare in that domain,
    # rather than accepting a tolerance that also admits distinct clipped f32s.
    f32 = lambda value: struct.unpack("f", struct.pack("f", value))[0]
    expanded = [f32(value) for value in values]
    if len(expanded) == 17:
        if expanded[5] <= -1 / 3:
            raise EngineError("invalid_params", "The FSRS parameter set is outside native inference bounds.")
        expanded[4] = f32(2 * expanded[5] + expanded[4])
        expanded[5] = f32(f32(math.log(f32(3 * expanded[5] + 1))) / 3)
        expanded[6] = f32(expanded[6] + .5)
        expanded.extend((0, 0, 0, .5))
    elif len(expanded) == 19:
        expanded.extend((0, .5))
    if expanded and any(not math.isfinite(value) or value < f32(low) or value > f32(high)
                        for value, (low, high) in zip(expanded, PARAMETER_BOUNDS)):
        raise EngineError("invalid_params", "The FSRS parameter set is outside native inference bounds.")
    return list(values)


def _checkpoint(engine):
    # E05 owns the persistent request flag and backend lifetime control. No
    # local cancellation flag may disagree with the shared lifecycle.
    engine.progress.check_abort()


@contextmanager
def _snapshot_path(engine, control=True):
    """SQLite's online backup includes WAL data without writing to the source.

    Native simulation/card statistics can cache memory/last-review data. Their
    disposable collection is the only place those nominal reads may write.
    """
    progress = engine.progress
    phase = progress.using_backend(None) if control else nullcontext()
    with tempfile.TemporaryDirectory(prefix="memr-srs-fsrs-") as root:
        path = Path(root) / "collection.anki2"
        with phase:
            if control:
                _checkpoint(engine)

            def copied(_status, remaining, total):
                if control:
                    progress.report(done=total - remaining, total=total, label="Copying collection for native FSRS")
                    _checkpoint(engine)
            source = sqlite3.connect(Path(engine.col.path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            target = sqlite3.connect(path)
            try:
                source.backup(target, pages=256, progress=copied)
            finally:
                target.close()
                source.close()
            yield path


@contextmanager
def _snapshot(engine, control=False):
    with _snapshot_path(engine, control) as path:
        scratch = Collection(str(path))
        try:
            with engine.progress.using_backend(scratch) if control else nullcontext():
                if control:
                    _checkpoint(engine)
                yield scratch
        finally:
            scratch.close()


def _write_worker_json(path, value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(data.encode()) > MAX_WORKER_BYTES:
        raise EngineError("unsupported", "The native FSRS result exceeds the bounded response.")
    staging = path.with_suffix(".next")
    staging.write_text(data, encoding="utf-8")
    staging.replace(path)


def _worker(job_path):
    """Private isolated compute entry; the parent owns every file and process."""
    job_path = Path(job_path)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    col = Collection(str(job_path.parent / "collection.anki2"))
    # Native history preparation caches per-card data. This disposable snapshot
    # needs no durable cache commits; syncing each row stalls overlay-backed
    # workers before they can publish their first native progress checkpoint.
    col.db.execute("pragma synchronous=normal")
    result = {}

    def compute():
        try:
            if job["kind"] == "optimize":
                value = col._backend.compute_fsrs_params(**job["params"])
                result["result"] = {"parameters": list(value.params), "trainingItems": value.fsrs_items,
                                    "healthCheckPassed": value.health_check_passed if value.HasField("health_check_passed") else None}
            elif job["kind"] == "evaluate":
                value = col._backend.evaluate_params(**job["params"])
                result["result"] = {"logLoss": value.log_loss, "rmse": value.rmse_bins}
            elif job["kind"] == "evaluateParameters":
                value = col._backend.evaluate_params_legacy(**job["params"])
                result["result"] = {"logLoss": value.log_loss, "rmse": value.rmse_bins}
            elif job["kind"] == "retentionWorkload":
                costs = col._backend.get_retention_workload(**job["params"])
                result["result"] = [{"retention": retention / 100, "cost": cost} for retention, cost in sorted(costs.items())]
            else:
                request = scheduler_pb2.SimulateFsrsReviewRequest(**job["params"])
                if job["kind"] == "optimalRetention":
                    result["result"] = {"desiredRetention": col._backend.compute_optimal_retention(request)}
                elif job["kind"] == "simulate":
                    value = col._backend.simulate_fsrs_review(request)
                    lengths = {len(value.daily_review_count), len(value.daily_new_count), len(value.daily_time_cost), len(value.accumulated_knowledge_acquisition)}
                    if lengths != {request.days_to_simulate}:
                        raise EngineError("internal", "The native simulator returned inconsistent series.")
                    result["result"] = {"days": [{"day": day, "reviews": reviews, "newCards": new_cards,
                        "memorizedCards": memorized, "costSeconds": seconds}
                        for day, (reviews, new_cards, seconds, memorized) in enumerate(zip(value.daily_review_count,
                            value.daily_new_count, value.daily_time_cost, value.accumulated_knowledge_acquisition))],
                        "totalReviews": sum(value.daily_review_count), "totalNewCards": sum(value.daily_new_count),
                        "totalCostSeconds": sum(value.daily_time_cost)}
                elif job["kind"] == "simulateWorkload":
                    value = col._backend.simulate_fsrs_workload(request)
                    if set(value.cost) != set(value.memorized) or set(value.cost) != set(value.review_count):
                        raise EngineError("internal", "The native workload simulator returned inconsistent series.")
                    result["result"] = {"reviewlessMemorizedCards": value.reviewless_end_memorized,
                        "points": [{"retention": retention / 100, "costSeconds": value.cost[retention],
                            "memorizedCards": value.memorized[retention], "reviews": value.review_count[retention]}
                            for retention in sorted(value.cost)]}
                else:
                    raise EngineError("internal", "Unknown private FSRS computation.")
        except Exception as exception:
            result["error"] = public_error(exception).as_dict()
    thread = threading.Thread(target=compute, name="native-fsrs-compute")
    try:
        thread.start()
        while thread.is_alive():
            detail = backend_progress(col.latest_progress())
            # Native Monte Carlo refinements can exceed their initial estimate.
            # Keep the actual counter and expose an unknown total once that
            # estimate is exhausted, rather than inventing a completion ratio.
            if detail["total"] is not None and detail["done"] > detail["total"]:
                detail["total"] = None
            if not detail["label"]:
                detail["label"] = "Native FSRS " + job["kind"]
            _write_worker_json(job_path.parent / "progress.json", detail)
            thread.join(.05)
        _write_worker_json(job_path.parent / "outcome.json", result)
    finally:
        thread.join()
        col.close()


def _stop_worker(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=.5)
    else:
        process.wait()


def _compute(engine, kind, params):
    """Terminate isolated native work when Anki lacks in-flight checkpoints.

    Pending aborts survive native progress initialization, and cancellation
    never kills the engine or writes its live collection. The same path covers
    unchecked optimizer comparison passes and time-series evaluation folds.
    """
    _checkpoint(engine)
    with _snapshot_path(engine) as path:
        job = path.parent / "job.json"
        _write_worker_json(job, {"kind": kind, "params": params})
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR") if key in os.environ}
        env["RAYON_NUM_THREADS"] = "2"
        code = "import sys;sys.path.insert(0,sys.argv[1]);from memr_srs_engine.handlers.fsrs import _worker;_worker(sys.argv[2])"
        _checkpoint(engine)
        process = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(Path(__file__).resolve().parents[2]), str(job)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=str(path.parent))
        deadline = time.monotonic() + MAX_COMPUTE_SECONDS
        try:
            while True:
                _checkpoint(engine)
                progress = path.parent / "progress.json"
                if progress.exists():
                    if progress.stat().st_size > 65536:
                        raise EngineError("internal", "The native FSRS progress is invalid.")
                    detail = json.loads(progress.read_text(encoding="utf-8"))
                    engine.progress.report(**detail)
                if process.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    raise EngineError("unsupported", "The native FSRS computation exceeded its bounded runtime; narrow the workload.")
                engine.stop.wait(.02)
            _checkpoint(engine)
            outcome = path.parent / "outcome.json"
            if process.returncode != 0 or not outcome.exists() or outcome.stat().st_size > MAX_WORKER_BYTES:
                raise EngineError("internal", "The native FSRS compute process could not complete.")
            result = json.loads(outcome.read_text(encoding="utf-8"))
            if "error" in result:
                error = result["error"]
                raise EngineError(error["code"], error["message"])
            _checkpoint(engine)
            return result["result"]
        finally:
            _stop_worker(process)


def _request_dict(request):
    result = {}
    for field, value in request.ListFields():
        result[field.name] = list(value) if field.is_repeated else value
    return result


def _selected(col, search):
    _text(search)
    selected = col.find_cards(search)
    if len(selected) > MAX_SELECTED_CARDS:
        raise EngineError("unsupported", "The FSRS search exceeds the bounded workload; narrow the search.")
    return selected


def _cutoff(value):
    if not value:
        return 0
    try:
        return int(datetime.combine(date.fromisoformat(value), datetime.min.time(), timezone.utc).timestamp())
    except ValueError:
        raise EngineError("invalid_params", "The preset history cutoff is not a valid date.") from None


def _preset_search(config, requested):
    if requested.strip():
        return requested
    if config.config.param_search.strip():
        return config.config.param_search
    escaped = config.name.replace("\\", "\\\\").replace('"', '\\"')
    return f'preset:"{escaped}" -is:suspended'


def _relearning_steps(steps):
    elapsed, count = 0, 0
    for step in steps:
        elapsed += step
        if elapsed < 1440:
            count += 1
    return count


@method("fsrs.historyCount")
def history_count(engine, params):
    _selected(engine.col, params["search"])
    _cutoff(params["ignoreReviewsBeforeDate"])
    value = engine.col._backend.get_ignored_before_count(search=params["search"],
        ignore_revlogs_before_date=params["ignoreReviewsBeforeDate"])
    return {"included": value.included, "total": value.total}


@method("fsrs.retentionWorkload")
def retention_workload(engine, params):
    _selected(engine.col, params["search"])
    values = _parameters(params["parameters"])
    value = _compute(engine, "retentionWorkload", {"w": values, "search": params["search"]})
    return {**mutation_result(undo_state(engine.col)), "fsrsRetentionWorkload": value}


@method("fsrs.optimize")
def optimize(engine, params):
    _checkpoint(engine)
    col = engine.col
    config = _preset(col, params["presetId"])
    search = _preset_search(config, params["search"])
    _selected(col, search)
    values = _parameters(config.config.fsrs_params_6 or config.config.fsrs_params_5 or config.config.fsrs_params_4)
    cutoff = params.get("ignoreReviewsBefore", _cutoff(config.config.ignore_revlogs_before_date))
    state = col.decks.get_deck_configs_for_update(1)
    engine.progress.report(label="Optimizing native FSRS parameters")
    _checkpoint(engine)
    result = _compute(engine, "optimize", {"search": search, "current_params": values,
        "ignore_revlogs_before_ms": cutoff * 1000, "num_of_relearning_steps": _relearning_steps(config.config.relearn_steps), "health_check": state.fsrs_health_check})
    _checkpoint(engine)
    parameters = result["parameters"]
    _parameters(parameters)
    config.config.fsrs_params_6[:] = parameters
    state = col.decks.get_deck_configs_for_update(1)
    if params.get("reschedule", False) and not state.fsrs:
        raise EngineError("invalid_params", "Rescheduling requires FSRS to be enabled.")
    selected = _preset(col, state.current_deck.config_id)
    if selected.id == config.id:
        selected = config
    configs = [config] if selected.id == config.id else [config, selected]
    request = _request(state, 1, configs, fsrs_reschedule=params.get("reschedule", False))
    with engine.progress.using_backend(col):
        _checkpoint(engine)
        changed = col.decks.update_deck_configs(request)
    # A successful native transaction has committed both data and undo. Do not
    # turn a late abort into a Python rollback with phantom native undo entries.
    return _result(col, changed, fsrsOptimization={"presetId": config.id, "parameters": parameters,
        "trainingItems": result["trainingItems"], "healthCheckPassed": result["healthCheckPassed"], "search": search})


@method("fsrs.evaluate")
def evaluate(engine, params):
    _checkpoint(engine)
    col, search = engine.col, params["search"]
    _selected(col, search)
    cutoff = params.get("ignoreReviewsBefore", 0) * 1000
    values = _parameters(params["parameters"]) if "parameters" in params else None
    engine.progress.report(label="Evaluating native FSRS history")
    _checkpoint(engine)
    arguments = {"search": search, "ignore_revlogs_before_ms": cutoff}
    arguments.update({"params": values} if values is not None else {"num_of_relearning_steps": 1})
    value = _compute(engine, "evaluateParameters" if values is not None else "evaluate", arguments)
    _checkpoint(engine)
    return {**mutation_result(undo_state(col)), "fsrsEvaluation": {**value,
            "mode": "parameters" if values is not None else "timeSeries", "parameters": values}}


@method("fsrs.optimizeAll")
def optimize_all(engine, params):
    col, deck_id = engine.col, params["deckId"]
    _normal_deck(col, deck_id)
    state = col.decks.get_deck_configs_for_update(deck_id)
    if not state.fsrs:
        raise EngineError("invalid_params", "Enable FSRS before optimizing all presets.")
    configs, optimized, failures = [], [], []
    for item in state.all_config:
        _checkpoint(engine)
        config = item.config
        search = _preset_search(config, "")
        try:
            _selected(col, search)
            result = _compute(engine, "optimize", {"search": search,
                "current_params": _parameters(config.config.fsrs_params_6),
                "ignore_revlogs_before_ms": _cutoff(config.config.ignore_revlogs_before_date) * 1000,
                "num_of_relearning_steps": _relearning_steps(config.config.relearn_steps), "health_check": False})
            config.config.fsrs_params_6[:] = _parameters(result["parameters"])
            configs.append(config)
            optimized.append({"presetId": config.id, "parameters": result["parameters"],
                "trainingItems": result["trainingItems"], "healthCheckPassed": result["healthCheckPassed"], "search": search})
        except Exception as exception:
            error = public_error(exception)
            if error.code not in ("invalid_params", "unsupported"):
                raise
            failures.append({"presetId": config.id, "message": error.message})
    _checkpoint(engine)
    if not configs:
        return {**mutation_result(undo_state(col)), "fsrsOptimizations": [], "fsrsOptimizationFailures": failures}
    # Compute everything before the first write. Cancellation during training
    # cannot leave partially optimized presets. One native undo restores all.
    selected = next((config for config in configs if config.id == state.current_deck.config_id),
                    _preset(col, state.current_deck.config_id))
    configs = [config for config in configs if config.id != selected.id] + [selected]
    changed = None

    def apply():
        nonlocal changed
        step = col.add_custom_undo_entry("Optimize all FSRS presets")
        col.decks.update_deck_configs(_request(state, deck_id, configs, fsrs_reschedule=params.get("reschedule", False)))
        col.set_config("lastFsrsOptimize", col.sched.today, undoable=True)
        changed = col.merge_undo_entries(step)
    with engine.progress.using_backend(col):
        _checkpoint(engine)
        col.db.transact(apply)
    return _result(col, changed, fsrsOptimizations=optimized, fsrsOptimizationFailures=failures)


def _deck_search(col, deck_id, extra):
    deck = _normal_deck(col, deck_id)
    selected = [deck_id] + [item.id for item in col.decks.all_names_and_ids() if item.name.startswith(deck["name"] + "::")]
    term = "did:" + ",".join(str(value) for value in selected)
    return f"({term}) ({extra})" if extra.strip() else term


def _simulation_request(col, params, search, defaults):
    values = _parameters(params.get("parameters", []))
    easy_days = params.get("easyDays", [])
    if len(easy_days) not in (0, 7) or any(value not in (0, .5, 1) for value in easy_days):
        raise EngineError("invalid_params", "Easy days require defaults or seven values chosen from 0, 0.5 and 1.")
    order = params.get("reviewOrder", "due")
    if order in ("added", "reverseAdded", "relativeOverdueness"):
        raise EngineError("unsupported", "The pinned native simulator does not support this review order.")
    selected = _selected(col, search)
    additional = params.get("additionalNewCards", 0)
    days = params.get("days", 365)
    if (len(selected) + additional) * days > MAX_SIMULATED_CARD_DAYS:
        raise EngineError("unsupported", "The simulation exceeds the bounded workload; reduce the days or cards.")
    # Native appends `introduced:1` when calculating new-card due dates. Keep
    # that clause outside every branch of a caller's OR expression.
    request = scheduler_pb2.SimulateFsrsReviewRequest(params=values, search=f"({search})" if search.strip() else "",
        desired_retention=params.get("desiredRetention", .9), deck_size=additional, days_to_simulate=days,
        new_limit=params.get("newCardsPerDay", defaults["new"]), review_limit=params.get("reviewLimit", defaults["review"]),
        max_interval=params.get("maximumIntervalDays", 36500), easy_days_percentages=easy_days,
        new_cards_ignore_review_limit=params.get("newIgnoresReviewLimit", defaults["ignore"]),
        review_order=REVIEW_ORDER.index(order), historical_retention=params.get("historicalRetention", .9),
        learning_step_count=params.get("learningSteps", defaults["learn"]), relearning_step_count=params.get("relearningSteps", defaults["relearn"]))
    if params.get("suspendAfterLapses") is not None:
        request.suspend_after_lapse_count = params["suspendAfterLapses"]
    return request


def _simulate(engine, params, workload=False):
    _checkpoint(engine)
    col = engine.col
    _normal_deck(col, params["deckId"])
    state = col.decks.get_deck_configs_for_update(params["deckId"])
    config = _preset(col, state.current_deck.config_id).config
    search = _deck_search(col, params["deckId"], "")
    defaults = {"new": config.new_per_day, "review": config.reviews_per_day, "ignore": state.new_cards_ignore_review_limit,
                "learn": len(config.learn_steps), "relearn": len(config.relearn_steps)}
    request = _simulation_request(col, params, search, defaults)
    _checkpoint(engine)
    value = _compute(engine, "simulateWorkload" if workload else "simulate", _request_dict(request))
    _checkpoint(engine)
    return {**mutation_result(undo_state(col)), "fsrsWorkload" if workload else "fsrsSimulation": value}


@method("fsrs.simulate")
def simulate(engine, params):
    return _simulate(engine, params)


@method("fsrs.simulateWorkload")
def simulate_workload(engine, params):
    return _simulate(engine, params, workload=True)


@method("fsrs.optimalRetention")
def optimal_retention(engine, params):
    _checkpoint(engine)
    # Search-only calls retain an explicit app-default policy: native deck
    # default daily limits, no additional new stock, a 365-day simulation.
    state = engine.col.decks.get_deck_configs_for_update(1)
    config = state.defaults.config
    settings = dict(params.get("settings", {}))
    if "parameters" in params:
        if "parameters" in settings and params["parameters"] != settings["parameters"]:
            raise EngineError("invalid_params", "Choose one FSRS parameter set for the simulation.")
        settings["parameters"] = params["parameters"]
    request = _simulation_request(engine.col, settings, params["search"], {"new": config.new_per_day, "review": config.reviews_per_day,
        "ignore": True, "learn": len(config.learn_steps), "relearn": len(config.relearn_steps)})
    _checkpoint(engine)
    value = _compute(engine, "optimalRetention", _request_dict(request))
    _checkpoint(engine)
    return {**mutation_result(undo_state(engine.col)), "fsrsOptimalRetention": value,
            "summary": f"Native optimal retention: {request.days_to_simulate} days, daily limits {request.new_limit}/{request.review_limit}, additional new cards {request.deck_size}."}


@method("fsrs.memoryState")
def memory_state(engine, params):
    card_id = params["cardId"]
    engine.col.get_card(card_id)
    with _snapshot(engine, control=False) as scratch:
        value = scratch._backend.compute_memory_state(card_id)
        if not value.HasField("state"):
            return None
        card = scratch.get_card(card_id)
        card.memory_state, card.desired_retention, card.decay = value.state, value.desired_retention, value.decay
        scratch.update_card(card)
        stats = scratch.card_stats_data(card_id)
    return {"stability": value.state.stability, "difficulty": value.state.difficulty,
            "retrievability": stats.fsrs_retrievability if stats.HasField("fsrs_retrievability") else None,
            "desiredRetention": value.desired_retention, "decay": value.decay}
