"""Process-local diagnostics; never retries, cancels or restarts business work."""

from __future__ import annotations

import asyncio
import threading
import time


class RuntimeHealth:
    HEARTBEAT_SECONDS = 5
    LOOP_STALE_SECONDS = 30
    PROGRESS_WARNING_SECONDS = 900
    API_WARNING_SECONDS = 180

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._started = self._clock()
        self._heartbeat = None
        self._closing = False
        self._tasks = {}
        self._cycles = {}
        self._bots = {}

    def close(self):
        with self._lock:
            self._closing = True

    async def heartbeat(self):
        while True:
            with self._lock:
                self._heartbeat = self._clock()
            await asyncio.sleep(self.HEARTBEAT_SECONDS)

    def create_task(self, name, coro):
        task = asyncio.create_task(coro, name=name)
        with self._lock:
            self._tasks[name] = {"task": task, "error": False}
        task.add_done_callback(lambda done: self._task_done(name, done))
        return task

    def _task_done(self, name, task):
        # Retrieve exceptions to avoid unobserved-task warnings. Never log payloads.
        error = task.cancelled() or task.exception() is not None
        with self._lock:
            if self._tasks[name]["task"] is task:
                self._tasks[name]["error"] = error

    def cycle_started(self, name):
        with self._lock:
            previous = self._cycles.get(name, {})
            self._cycles[name] = {
                **previous, "progress": self._clock(), "active": True,
                "stage": "starting", "step_errors": [],
            }

    def progress(self, name, stage):
        with self._lock:
            self._cycles[name].update(progress=self._clock(), stage=stage)

    def step_failed(self, name, step):
        with self._lock:
            errors = self._cycles[name]["step_errors"]
            if step not in errors:
                errors.append(step)

    def cycle_finished(self, name, error=False):
        with self._lock:
            state = self._cycles[name]
            state.update(progress=self._clock(), active=False, error=error)
            if not error:
                state["success"] = self._clock()

    def expect_bot(self, name, expected):
        with self._lock:
            if expected:
                self._bots[name] = {
                    "expected": True, "task": None, "error": False,
                    "requests": {}, "last_success": None,
                }
            else:
                self._bots.setdefault(name, {"requests": {}, "last_success": None})["expected"] = False

    def bot_start_failed(self, name):
        with self._lock:
            self._bots[name]["start_failed"] = True

    def track_bot(self, name, task):
        with self._lock:
            self._bots[name]["task"] = task

    def api_middleware(self, name):
        async def observe(make_request, bot, method):
            marker = object()
            with self._lock:
                state = self._bots[name]
                state["requests"][marker] = self._clock()
            try:
                result = await make_request(bot, method)
            except asyncio.CancelledError:
                raise
            except Exception:
                with self._lock:
                    state["error"] = True
                raise
            else:
                with self._lock:
                    state.update(error=False, last_success=self._clock())
                return result
            finally:
                with self._lock:
                    state["requests"].pop(marker, None)
        return observe

    def snapshot(self):
        with self._lock:
            now = self._clock()
            age = lambda value: None if value is None else round(max(0, now - value), 1)
            failed = []
            warnings = []
            missing = {"heartbeat", "payments", "subscriptions", "notifications", "runtime_watchdog"} - self._tasks.keys()
            if missing and not self._closing:
                warnings.append(
                    "startup_delayed"
                    if now - self._started > self.PROGRESS_WARNING_SECONDS
                    else "startup_incomplete"
                )
            heartbeat_age = age(self._heartbeat)
            if heartbeat_age is not None and heartbeat_age > self.LOOP_STALE_SECONDS:
                failed.append("event_loop_stalled")
            tasks = {}
            for name, entry in self._tasks.items():
                done = entry["task"].done()
                tasks[name] = ("failed" if entry["error"] else "stopped") if done else "running"
                if done and not self._closing:
                    failed.append(name + "_stopped")
            cycles = {}
            for name, state in self._cycles.items():
                progress_age = age(state["progress"])
                cycles[name] = {
                    "active": state["active"], "stage": state["stage"],
                    "progress_age_seconds": progress_age,
                    "success_age_seconds": age(state.get("success")),
                    "last_cycle_error": state.get("error", False),
                    "step_errors": list(state.get("step_errors", [])),
                }
                if state.get("error"):
                    warnings.append(name + "_cycle_error")
                if progress_age > self.PROGRESS_WARNING_SECONDS:
                    warnings.append(name + "_progress_delayed")
            bots = {}
            for name, state in self._bots.items():
                if not state["expected"]:
                    bots[name] = {"status": "stopped_intentionally"}
                    continue
                task = state.get("task")
                status = (
                    ("start_failed" if state.get("start_failed") else "starting")
                    if task is None
                    else ("stopped" if task.done() else "running")
                )
                if status == "start_failed":
                    warnings.append(name + "_start_failed")
                bots[name] = {"status": status, "last_api_success_age_seconds": age(state["last_success"])}
                if status == "stopped" and not self._closing:
                    failed.append(name + "_stopped")
                if state.get("error"):
                    warnings.append(name + "_api_error")
                requests = state["requests"].values()
                if requests and now - min(requests) > self.API_WARNING_SECONDS:
                    warnings.append(name + "_api_delayed")
            return {
                "failed": failed, "warnings": warnings, "tasks": tasks,
                "cycles": cycles, "bots": bots, "heartbeat_age_seconds": heartbeat_age,
                "closing": self._closing,
            }
