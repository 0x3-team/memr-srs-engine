"""Native FSRS computations, complete terminal results and cooperative abort."""
import json
import threading
import time
from datetime import datetime
from unittest.mock import patch

from anki.collection import AddNoteRequest

from memr_srs_engine.validate import validate_value
from memr_srs_engine.handlers import fsrs
from tests.e06_helpers import RpcCase


def finish_operation(case, operation_id, timeout=90):
    deadline = time.monotonic() + timeout
    while case.engine.progress.thread.is_alive() and time.monotonic() < deadline:
        case.engine.progress.tick()
        case.engine.progress.thread.join(.02)
    case.assertFalse(case.engine.progress.thread.is_alive(), "Native operation must settle within its test deadline")
    case.engine.progress.tick()
    return case.rpc("progress.get", {"operationId": operation_id})


class FsrsTests(RpcCase):
    def history(self, count=100, reviews=16):
        requests = []
        model = self.col.models.by_name("Basic")
        for index in range(count):
            note = self.col.new_note(model)
            note["Front"], note["Back"] = f"History {index}", "Generated answer"
            requests.append(AddNoteRequest(note=note, deck_id=1))
        self.col.add_notes(requests)
        cards = [self.col.get_card(cid) for cid in self.col.find_cards("")]
        start = (self.col.sched.day_cutoff - (reviews * 3 + 2) * 86400) * 1000
        rows = []
        for index, card in enumerate(cards):
            for review in range(reviews):
                rating = (1, 3, 3, 2, 4, 3, 1, 3)[(index + review) % 8]
                rows.append((start + review * 3 * 86400000 + index, card.id, -1, rating,
                             3 if rating != 1 else -600, 3, 2500, 2000 + index, 0 if review == 0 else 1))
            card.type, card.queue, card.ivl, card.reps, card.factor = 2, 2, 3, reviews, 2500
            card.due = self.col.sched.today + 1
        # Keep all generated history while avoiding an fsync per review on CI's
        # overlay filesystem. DBProxy.executemany does not begin a transaction.
        self.col.db.execute("begin")
        try:
            self.col.db.executemany("insert into revlog values (?,?,?,?,?,?,?,?,?)", rows)
        except BaseException:
            self.col.db.execute("rollback")
            raise
        else:
            self.col.db.execute("commit")
        self.col.update_cards(cards)
        return cards

    def start(self, method, params):
        frame = self.frame(method, params)
        self.assertIn("result", frame, frame)
        ack = frame["result"]
        self.assertEqual(validate_value(self.engine.registry.catalogue[method]["result"], ack), [])
        return ack["operationId"]

    def finish(self, operation_id, timeout=90):
        return finish_operation(self, operation_id, timeout)

    def long_call(self, method, params):
        result = self.finish(self.start(method, params))
        self.assertEqual(result["status"], "completed", result)
        return result["result"]

    def simulation(self, **overrides):
        return {"deckId": 1, "parameters": [], "desiredRetention": .9, "days": 10,
                "newCardsPerDay": 20, "reviewLimit": 200, "maximumIntervalDays": 36500,
                "easyDays": [], **overrides}

    def test_all_methods_registered(self):
        expected = {name for name in self.engine.registry.catalogue if name.startswith("fsrs.")}
        self.assertEqual(expected & self.engine.registry.handlers.keys(), expected)

    def test_memory_state_absent_for_new_card_and_missing_card(self):
        card = self.note(1).cards()[0]
        self.assertIsNone(self.rpc("fsrs.memoryState", {"cardId": card.id}))
        self.error("fsrs.memoryState", {"cardId": 999}, "not_found")
        self.error("fsrs.memoryState", {"cardId": True}, "invalid_params")

    def test_invalid_long_parameters_rejected_before_operation(self):
        for method, params in [
            ("fsrs.optimize", {"presetId": -1, "search": ""}),
            ("fsrs.evaluate", {"search": "", "parameters": [float("nan")]}),
            ("fsrs.simulate", {"deckId": 1}),
            ("fsrs.simulate", self.simulation(easyDays=[1, 1, 1, 1, 1, .25, .5])),
            ("fsrs.optimalRetention", {}),
        ]:
            with self.subTest(method=method):
                self.error(method, params, "invalid_params")
                self.assertFalse(self.engine.progress.running)

    def test_optimization_evaluation_and_native_undo_redo(self):
        self.history(count=1000, reviews=60)
        before = self.col.decks.get_config(1)
        outcome = self.long_call("fsrs.optimize", {"presetId": 1, "search": ""})
        optimized = outcome["fsrsOptimization"]
        self.assertGreater(optimized["trainingItems"], 0)
        self.assertEqual(len(optimized["parameters"]), 21)
        self.assertEqual(optimized["search"], 'preset:"Default" -is:suspended')
        self.assertTrue(outcome["changes"]["deckConfig"])
        self.assertTrue(outcome["undo"]["canUndo"])
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertTrue(any(frame.get("event") == "progress" and frame["progress"]["status"] == "running"
                            and frame["progress"]["done"] > 0 for frame in events),
                        "Generated review history must expose actual native optimization progress")
        self.assertEqual(list(self.col._backend.get_deck_config(1).config.fsrs_params_6), optimized["parameters"])
        self.col.undo()
        self.assertEqual(self.col.decks.get_config(1), before)
        self.col.redo()
        parameters = optimized["parameters"]
        status = self.col.undo_status()
        evaluated = self.long_call("fsrs.evaluate", {"search": "", "parameters": parameters})["fsrsEvaluation"]
        native = self.col._backend.evaluate_params_legacy(params=parameters, search="", ignore_revlogs_before_ms=0)
        self.assertEqual((evaluated["mode"], evaluated["parameters"]), ("parameters", parameters))
        # Rayon reduction order can differ between the two-thread isolated
        # worker and this direct native call; compare their f32 metrics to 1e-6.
        self.assertAlmostEqual(evaluated["logLoss"], native.log_loss, places=6)
        self.assertAlmostEqual(evaluated["rmse"], native.rmse_bins, places=6)
        self.assertEqual(self.col.undo_status(), status)

    def test_optimizer_preserves_absent_health_check_and_late_commit_abort(self):
        self.history(count=1000, reviews=60)
        options = self.rpc("deckOptions.get", {"deckId": 1})
        options["config"]["fsrs"]["parameters"] = list(self.col.decks.get_deck_configs_for_update(1).defaults.config.fsrs_params_6)
        options["config"]["fsrs"]["parameters"][0] = .123
        params = {key: options[key] for key in ("deckId", "config", "deckLimits", "todayLimits", "fsrsEnabled")}
        self.long_call("deckOptions.update", {**params, "applyToChildren": False, "fsrsHealthCheck": False})
        before = self.col.decks.get_config(1)
        original = self.col.decks.update_deck_configs

        def after_commit(request):
            changes = original(request)
            self.engine.progress.abort(self.engine.progress.current)
            return changes
        with patch.object(self.col.decks, "update_deck_configs", after_commit):
            value = self.long_call("fsrs.optimize", {"presetId": 1, "search": ""})
        self.assertIsNone(value["fsrsOptimization"]["healthCheckPassed"])
        self.assertTrue(value["changes"]["deckConfig"])
        self.assertTrue(value["undo"]["canUndo"])
        self.col.undo()
        self.assertEqual(self.col.decks.get_config(1), before)
        self.col.redo()
        self.assertEqual(list(self.col._backend.get_deck_config(1).config.fsrs_params_6), value["fsrsOptimization"]["parameters"])
        self.assertIsInstance(self.rpc("stats.today")["reviews"], int)

    def test_native_time_series_evaluation_reports_its_actual_mode(self):
        self.history(count=100, reviews=24)
        history = self.col.undo_status()
        value = self.long_call("fsrs.evaluate", {"search": ""})["fsrsEvaluation"]
        self.assertEqual(value["mode"], "timeSeries")
        self.assertIsNone(value["parameters"])
        self.assertGreaterEqual(value["logLoss"], 0)
        self.assertTrue(0 <= value["rmse"] <= 1)
        self.assertEqual(self.col.undo_status(), history)

    def test_simulation_and_optimal_retention_do_not_persist_native_caches(self):
        cards = self.history(count=4)
        before = self.col.db.all("select * from cards order by id")
        status = self.col.undo_status()
        result = self.long_call("fsrs.simulate", self.simulation())["fsrsSimulation"]
        self.assertEqual([day["day"] for day in result["days"]], list(range(10)))
        self.assertEqual(result["totalReviews"], sum(day["reviews"] for day in result["days"]))
        self.assertEqual(result["totalNewCards"], sum(day["newCards"] for day in result["days"]))
        self.assertEqual(result["totalCostSeconds"], sum(day["costSeconds"] for day in result["days"]))
        self.assertEqual(self.col.db.all("select * from cards order by id"), before)
        self.assertEqual(self.col.undo_status(), status)
        memory = self.rpc("fsrs.memoryState", {"cardId": cards[0].id})
        native = self.col.compute_memory_state(cards[0].id)
        self.assertEqual((memory["stability"], memory["difficulty"], memory["decay"]), (native.stability, native.difficulty, native.decay))
        self.assertTrue(memory["retrievability"] is None or 0 <= memory["retrievability"] <= 1)
        retention = self.long_call("fsrs.optimalRetention", {"search": ""})["fsrsOptimalRetention"]["desiredRetention"]
        self.assertTrue(.7 - 1e-6 <= retention <= .95 + 1e-6)
        self.assertEqual(self.col.db.all("select * from cards order by id"), before)
        self.assertEqual(self.col.undo_status(), status)

    def test_easy_days_simulation_redistributes_reviews_off_the_minimum_weekday(self):
        self.history(count=60, reviews=24)

        def reviews_per_weekday(easy_days):
            result = self.long_call("fsrs.simulate", self.simulation(days=14, newCardsPerDay=0,
                easyDays=easy_days, learningSteps=0, relearningSteps=0))["fsrsSimulation"]
            today = datetime.fromtimestamp(self.col.sched.day_cutoff - 86400).date()
            totals = [0] * 7
            for day in result["days"]:
                totals[(today.weekday() + day["day"]) % 7] += day["reviews"]
            return totals

        baseline = reviews_per_weekday([1] * 7)
        target = max(range(7), key=baseline.__getitem__)
        easy = [1] * 7
        easy[target] = 0
        shifted = reviews_per_weekday(easy)
        self.assertGreater(baseline[target], 0)
        self.assertLess(shifted[target], baseline[target] * .85,
                        f"Minimum weight on weekday index {target} must move simulated reviews off it: "
                        f"{baseline} -> {shifted}")

    def test_missing_and_invalid_long_results_are_safe_contract_errors(self):
        defaults = list(self.col.decks.get_deck_configs_for_update(1).defaults.config.fsrs_params_6)
        clipped = []
        for index, value in ((0, .00099995), (17, -.00000005)):
            parameters = defaults[:]
            parameters[index] = value
            clipped.append(("fsrs.evaluate", {"search": "", "parameters": parameters}, "invalid_params"))
        for method, params, code in [
            ("fsrs.optimize", {"presetId": 999, "search": ""}, "not_found"),
            ("fsrs.evaluate", {"search": "("}, "invalid_params"),
            ("fsrs.evaluate", {"search": "", "parameters": [1]}, "invalid_params"),
            ("fsrs.simulate", self.simulation(deckId=999), "not_found"),
            ("fsrs.simulate", self.simulation(easyDays=[1, 1]), "invalid_params"),
            *(("fsrs.simulate", self.simulation(reviewOrder=order), "unsupported")
              for order in ("added", "reverseAdded", "relativeOverdueness")),
            ("fsrs.simulate", self.simulation(days=36500, additionalNewCards=100000), "unsupported"),
            ("fsrs.optimalRetention", {"search": "("}, "invalid_params"),
            *clipped,
        ]:
            with self.subTest(method=method, code=code):
                outcome = self.finish(self.start(method, params))
                self.assertEqual(outcome["status"], "failed", outcome)
                self.assertEqual(outcome["error"]["code"], code)
                self.assertNotIn("Traceback", str(outcome))

    def test_native_simulator_keeps_search_clause_grouped(self):
        cards = self.history(count=2)
        note = self.col.get_note(cards[0].nid)
        note.tags = ["left"]
        self.col.update_note(note)
        defaults = {"new": 20, "review": 200, "ignore": True, "learn": 2, "relearn": 1}
        request = fsrs._simulation_request(self.col, self.simulation(), "tag:left OR tag:absent", defaults)
        self.assertEqual(request.search, "(tag:left OR tag:absent)")
        self.assertEqual(self.col.find_cards(request.search), [cards[0].id])
        self.assertEqual(self.col.find_cards(request.search + " introduced:1"), [])
        self.assertEqual(self.col.find_cards("tag:left OR tag:absent introduced:1"), [cards[0].id])

    def test_distinct_out_of_bounds_floats_are_rejected_before_compute(self):
        defaults = list(self.col.decks.get_deck_configs_for_update(1).defaults.config.fsrs_params_6)
        with patch.object(fsrs, "_compute", side_effect=AssertionError("Invalid weights reached native computation")) as compute:
            for index, invalid in ((0, .00099995), (17, -.00000005)):
                parameters = defaults[:]
                parameters[index] = invalid
                outcome = self.finish(self.start("fsrs.evaluate", {"search": "", "parameters": parameters}))
                self.assertEqual((outcome["status"], outcome["error"]["code"]), ("failed", "invalid_params"))
            compute.assert_not_called()

    def test_all_long_methods_abort_after_real_native_work_without_saving(self):
        self.history(count=40)
        before_cards = self.col.db.all("select * from cards order by id")
        before_preset = self.col.decks.get_config(1)
        for method, params in [
            ("fsrs.optimize", {"presetId": 1, "search": ""}),
            ("fsrs.evaluate", {"search": "", "parameters": []}),
            ("fsrs.simulate", self.simulation()),
            ("fsrs.optimalRetention", {"search": ""}),
        ]:
            with self.subTest(method=method):
                completed, release = threading.Event(), threading.Event()
                original = fsrs._compute

                def at_native_boundary(instance, *args, **kwargs):
                    value = original(instance, *args, **kwargs)
                    completed.set()
                    if not release.wait(10):
                        raise RuntimeError("Test cancellation boundary was not released")
                    return value
                with patch.object(fsrs, "_compute", at_native_boundary):
                    operation_id = self.start(method, params)
                    try:
                        deadline = time.monotonic() + 60
                        while not completed.is_set() and self.engine.progress.thread.is_alive() and time.monotonic() < deadline:
                            completed.wait(.02)
                        self.assertTrue(completed.is_set(), self.engine.progress.get(operation_id))
                        self.engine.progress.tick()
                        self.error("stats.today", {}, "busy")
                        self.rpc("progress.abort", {"operationId": operation_id})
                    finally:
                        release.set()
                    outcome = self.finish(operation_id)
                self.assertEqual(outcome["status"], "aborted", outcome)
                self.assertEqual(outcome["error"]["code"], "aborted")
                self.assertEqual(self.col.db.all("select * from cards order by id"), before_cards)
                self.assertEqual(self.col.decks.get_config(1), before_preset)
                self.assertIsInstance(self.rpc("stats.today")["reviews"], int)

    def test_real_inflight_simulator_abort_reaps_owned_process_promptly(self):
        self.history(count=200, reviews=40)
        before = self.col.db.all("select * from cards order by id")
        history = self.col.undo_status()
        children = []
        original = fsrs.subprocess.Popen

        def track(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        parameters = [low for low, _high in fsrs.PARAMETER_BOUNDS]
        with patch.object(fsrs.subprocess, "Popen", track):
            operation_id = self.start("fsrs.simulate", self.simulation(parameters=parameters, days=9000,
                additionalNewCards=5000, desiredRetention=.99, newCardsPerDay=9999, reviewLimit=99999))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.engine.progress.tick()
                state = self.engine.progress.get(operation_id)
                if state["label"].startswith("Native FSRS simulate") and children and children[0].poll() is None:
                    break
                if not self.engine.progress.thread.is_alive():
                    self.fail(f"The generated in-flight workload ended before cancellation: {state}")
                self.engine.stop.wait(.01)
            else:
                self.fail("Native simulator never reached its real compute phase")
            started = time.monotonic()
            self.rpc("progress.abort", {"operationId": operation_id})
            result = self.finish(operation_id, timeout=3)
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "aborted", result)
        self.assertTrue(children)
        self.assertLess(children[0].returncode, 0, "The active native process must actually be terminated and reaped")
        self.assertEqual(self.col.db.all("select * from cards order by id"), before)
        self.assertEqual(self.col.undo_status(), history)
        self.assertIsInstance(self.rpc("stats.today")["reviews"], int)

    def test_each_history_compute_aborts_while_native_child_is_active(self):
        # Keep legacy evaluation active across its progress publication and
        # cancellation; the smaller fixture can finish in that interval.
        self.history(count=5000, reviews=60)
        self.create("Undo before cancellation")
        self.create("Redo before cancellation")
        self.col.undo()
        before_cards = self.col.db.all("select * from cards order by id")
        before_preset, history = self.col.decks.get_config(1), self.col.undo_status()
        settings = {key: value for key, value in self.simulation(days=1000, additionalNewCards=10000).items() if key != "deckId"}
        settings.update(newIgnoresReviewLimit=True, reviewOrder="due", suspendAfterLapses=None,
                        historicalRetention=.9, learningSteps=2, relearningSteps=1)
        for method, params in [
            ("fsrs.optimize", {"presetId": 1, "search": ""}),
            ("fsrs.evaluate", {"search": "", "parameters": []}),
            ("fsrs.evaluate", {"search": ""}),
            ("fsrs.optimalRetention", {"search": "", "settings": settings}),
        ]:
            with self.subTest(method=method, mode="parameters" if "parameters" in params else "native"):
                children = []
                original = fsrs.subprocess.Popen

                def track(*args, **kwargs):
                    child = original(*args, **kwargs)
                    children.append(child)
                    return child
                with patch.object(fsrs.subprocess, "Popen", track):
                    operation_id = self.start(method, params)
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        self.engine.progress.tick()
                        state = self.engine.progress.get(operation_id)
                        # Time-series native progress begins at 0/0, and is
                        # advanced only after an entire training fold finishes.
                        entered = state["total"] is not None if method == "fsrs.evaluate" and "parameters" not in params else state["done"] > 0
                        if method == "fsrs.evaluate" and "parameters" in params:
                            entered = state["total"] is not None and 0 < state["done"] < state["total"]
                        if (state["label"].startswith("Native FSRS") and entered
                                and children and children[0].poll() is None):
                            break
                        if not self.engine.progress.thread.is_alive():
                            self.fail(f"Native history computation ended before in-flight cancellation: {state}")
                        self.engine.stop.wait(.005)
                    else:
                        self.fail(f"Native history computation did not report progress: {state}")
                    started = time.monotonic()
                    self.rpc("progress.abort", {"operationId": operation_id})
                    result = self.finish(operation_id, timeout=3)
                    self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(result["status"], "aborted", result)
                self.assertLess(children[0].returncode, 0)
                self.assertEqual(self.col.db.all("select * from cards order by id"), before_cards)
                self.assertEqual(self.col.decks.get_config(1), before_preset)
                self.assertEqual(self.col.undo_status(), history)
                self.assertIsInstance(self.rpc("stats.today")["reviews"], int)

    def test_compute_timeout_and_child_failure_do_not_mutate_the_collection(self):
        self.history(count=4)
        cards, history = self.col.db.all("select * from cards order by id"), self.col.undo_status()
        with patch.object(fsrs, "MAX_COMPUTE_SECONDS", 0):
            result = self.finish(self.start("fsrs.simulate", self.simulation()))
        self.assertEqual((result["status"], result["error"]["code"]), ("failed", "unsupported"))
        original = fsrs.subprocess.Popen

        def failed_child(args, **kwargs):
            return original([args[0], "-I", "-B", "-c", "raise SystemExit(7)"], **kwargs)
        with patch.object(fsrs.subprocess, "Popen", failed_child):
            result = self.finish(self.start("fsrs.simulate", self.simulation()))
        self.assertEqual((result["status"], result["error"]["code"]), ("failed", "internal"))
        self.assertNotIn("Traceback", str(result))
        self.assertEqual(self.col.db.all("select * from cards order by id"), cards)
        self.assertEqual(self.col.undo_status(), history)
        self.assertIsInstance(self.rpc("stats.today")["reviews"], int)
