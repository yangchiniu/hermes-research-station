"""hermes-core plugin hooks — Runtime Hot Path Integration.

All callbacks are wrapped in try/except by PluginManager.invoke_hook(), so
a failure here will not crash Hermes.

Upgraded pipeline:
    on_session_start → init RuntimeHotPath + EventBus
    post_tool_call   → Telemetry → Reflection → Experience → Memory → DriftAnalyzer
    post_llm_call    → LLM latency telemetry
    on_session_end   → flush + summary + cleanup
"""

from __future__ import annotations

import json
import logging
import random
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level lazy state
# ---------------------------------------------------------------------------

_runtime = None
_session_start_time: float = 0.0
_call_timers: dict = {}  # tool_call_id → start_time for duration tracking

# Goal auto-tracking index — tool_name → goal_id (exact match, O(1))
_GOAL_TOOL_INDEX: dict[str, str] = {}

# Latency profiling — stage timings for post_tool_call pipeline
_profile_lock = threading.Lock()
_profile_counts: dict[str, int] = {}       # stage_name → call count
_profile_total: dict[str, float] = {}      # stage_name → total seconds
_profile_log_interval = 20                 # log summary every N calls
_profile_call_count = 0


def _profile_stage(stage_name: str, elapsed_s: float) -> None:
    """Record timing for a pipeline stage (thread-safe)."""
    with _profile_lock:
        _profile_counts[stage_name] = _profile_counts.get(stage_name, 0) + 1
        _profile_total[stage_name] = _profile_total.get(stage_name, 0.0) + elapsed_s


def _log_profile_summary() -> None:
    """Log aggregate latency profile and reset counters."""
    global _profile_call_count
    with _profile_lock:
        if not _profile_counts:
            return
        parts = []
        for name in sorted(_profile_counts):
            n = _profile_counts[name]
            t = _profile_total[name]
            avg_ms = (t / n) * 1000.0
            pct = (t / _profile_total.get("_total_hook", 1.0)) * 100.0 if "_total_hook" in _profile_total else 0.0
            parts.append(f"{name}={avg_ms:.1f}ms({pct:.0f}%)")
        logger.info(
            "hermes-core: latency profile [%d calls]: %s",
            _profile_call_count,
            " | ".join(parts),
        )
        # Also write to events table if available
        try:
            _eb_mod = sys.modules.get("event_bus")
            if _eb_mod and hasattr(_eb_mod, "get_bus"):
                bus = _eb_mod.get_bus()
                if bus and hasattr(bus, "publish"):
                    bus.publish("profile.latency", {
                        "call_count": _profile_call_count,
                        "stages": {k: {"avg_ms": round((_profile_total[k] / v) * 1000, 1)}
                                   for k, v in _profile_counts.items()},
                    })
        except Exception:
            pass
        _profile_counts.clear()
        _profile_total.clear()
        _profile_call_count = 0


def _ensure_runtime() -> None:
    """Lazy-init the RuntimeHotPath bridge."""
    global _runtime
    if _runtime is None:
        try:
            from .runtime_integration import get_runtime
            _runtime = get_runtime()
            logger.info("hermes-core: RuntimeHotPath initialised")
        except Exception as exc:
            logger.warning("hermes-core: RuntimeHotPath init failed: %s", exc)


# ---------------------------------------------------------------------------
# Helper: extract memory context summary for reflection passing
# ---------------------------------------------------------------------------


def _safe_truncate(text: str, max_len: int = 500) -> str:
    if not text:
        return ""
    return text[:max_len] + "..." if len(text) > max_len else text


# ---------------------------------------------------------------------------
# Hook: on_session_start
# ---------------------------------------------------------------------------


def on_session_start(session_id: str = "", **_: Any) -> None:
    """Hook invoked at the start of every Hermes session.

    Initialises the RuntimeHotPath, EventBus connectivity,
    and registers EventBus listeners.
    """
    global _session_start_time
    _session_start_time = time.time()

    # Initialise RuntimeHotPath
    _ensure_runtime()

    # Log session start — done via RuntimeHotPath
    logger.info("hermes-core: session %s started", session_id)

    # Register EventBus listeners for cross-module signalling
    try:
        if _runtime:
            from .__init__ import _lazy_core as _lc

            try:
                _bus_mod = _lc("event_bus")
                bus = _bus_mod.get_bus() if _bus_mod else None
                if bus and hasattr(bus, "subscribe"):
                    bus.subscribe("tool.completed", _on_tool_completed_event)
                    bus.subscribe("tool.failed", _on_tool_failed_event)
                    bus.subscribe("recovery.triggered", _on_recovery_event)
                    logger.info("hermes-core: registered EventBus listeners")
            except Exception as exc:
                logger.debug("hermes-core: EventBus listener registration: %s", exc)
    except Exception as exc:
        logger.debug("hermes-core: EventBus setup outer: %s", exc)

    logger.info("hermes-core: session %s started", session_id)


# ---------------------------------------------------------------------------
# EventBus event handlers
# ---------------------------------------------------------------------------


def _on_tool_completed_event(event_data) -> None:
    """Called when a tool.completed event fires on EventBus."""
    _ensure_runtime()
    if _runtime is None:
        return

    # event_data is an Event dataclass; payload lives in .data
    tool = event_data.data.get("tool", "unknown")
    task_id = event_data.data.get("task_id", "")
    duration = event_data.data.get("duration_s", 0)

    # RuntimeHotPath handles the audit trail internally


def _on_tool_failed_event(event_data) -> None:
    """Called when a tool.failed event fires."""
    _ensure_runtime()
    if _runtime is None:
        return

    tool = event_data.data.get("tool", "unknown")
    task_id = event_data.data.get("task_id", "")
    violations = event_data.data.get("violations", [])

    try:
        status = _runtime.get_status()
        mode = status.get("behavior_mode", "unknown")
        stability = status.get("stability_score", 1.0)

        # Log warning for any failure
        logger.warning(
            "hermes-core: tool.failed event — tool=%s mode=%s stability=%.2f violations=%d",
            tool, mode, stability, len(violations),
        )

        # In safe/frozen mode, trigger self-observation for diagnosis
        if mode in ("safe", "frozen"):
            if _runtime._self_obs is not None and hasattr(_runtime._self_obs, "run_once"):
                try:
                    report = _runtime._self_obs.run_once()
                    if report:
                        logger.info(
                            "hermes-core: triggered self-observation after failure in %s mode", mode,
                        )
                except Exception as exc:
                    logger.debug("hermes-core: self_obs run_once in tool.failed handler: %s", exc)
    except Exception as exc:
        logger.debug("hermes-core: tool.failed handler: %s", exc)


def _on_recovery_event(event_data) -> None:
    """Called when recovery.triggered event fires."""
    _ensure_runtime()
    logger.info("hermes-core: recovery triggered via EventBus")

    if _runtime is None:
        return

    # Trigger self-observation for post-recovery diagnosis
    try:
        if _runtime._self_obs is not None and hasattr(_runtime._self_obs, "run_once"):
            report = _runtime._self_obs.run_once()
            if report:
                logger.info("hermes-core: post-recovery self-observation completed")
                if hasattr(report, "alerts") and report.alerts:
                    logger.warning(
                        "hermes-core: post-recovery alerts: %s",
                        "; ".join(report.alerts[:3]),
                    )
    except Exception as exc:
        logger.debug("hermes-core: post-recovery self_observation: %s", exc)

    # Log recovery event — RuntimeHotPath handles audit trail internally
    
# ---------------------------------------------------------------------------
# Hook: pre_tool_call — Standard Tool HotPath Integration
# ---------------------------------------------------------------------------

_standard_tools = {
    "terminal", "read_file", "write_file", "search_files", "patch",
    "execute_code", "browser_navigate", "browser_click", "browser_type",
    "browser_snapshot", "browser_scroll", "browser_back", "browser_vision",
    "browser_console", "browser_get_images", "browser_press",
    "web_search", "web_extract", "session_search",
    "memory", "skill_view", "skill_manage", "skills_list",
    "delegate_task", "cronjob", "todo", "clarify", "send_message",
    "text_to_speech", "process",
}


def pre_tool_call(
    tool_name: str = "",
    args: Optional[dict] = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> Optional[dict]:
    """Called BEFORE every Hermes tool invocation.

    For standard Hermes tools (not hermes-core tools), runs:
        1. PolicyEngine.check_action() — is this action allowed?
        2. WorldModel snapshot — log environment state
        3. Telemetry — record tool attempt

    Returns None to allow execution, or {"action": "block", "message": "..."}
    to block the tool call.
    """
    if not tool_name:
        return None

    # Skip hermes-core tools — they already go through execute_tool() pipeline
    if tool_name.startswith("core_"):
        return None

    # Only intercept standard tools that need runtime supervision
    if tool_name not in _standard_tools:
        return None

    _ensure_runtime()
    if _runtime is None:
        return None

    # Record start time for duration tracking
    _call_timers[tool_call_id or tool_name] = time.time()

    # Total hook timer
    _t0 = time.time()

    # ---- 1. PolicyEngine check ----
    try:
        if _runtime._policy is not None:
            policy_result = _runtime._policy.check_action(tool_name, args or {})
            if not policy_result.get("allowed", True):
                reason = policy_result.get("reason", "Policy denied")
                logger.info(
                    "hermes-core: pre_tool blocked %s — %s",
                    tool_name, reason,
                )
                return {"action": "block", "message": f"[PolicyDenied] {reason}"}
    except Exception as exc:
        logger.debug("hermes-core: PolicyEngine check: %s", exc)

    # ---- 2. WorldModel snapshot (async, best-effort) ----
    try:
        if _runtime._world_model is not None:
            _runtime._world_model.get_world_state(refresh=True)
    except Exception as exc:
        logger.debug("hermes-core: WorldModel snapshot: %s", exc)

    # ---- 3. Watchdog deadlock check (periodic ~10%) ----
    try:
        if random.random() < 0.1:
            if _runtime._watchdog is not None and hasattr(_runtime._watchdog, "check_deadlocks"):
                deadlocks = _runtime._watchdog.check_deadlocks()
                if deadlocks:
                    logger.warning("hermes-core: watchdog detected %d deadlocks", len(deadlocks))
    except Exception as exc:
        logger.debug("hermes-core: Watchdog deadlock check: %s", exc)

    _profile_stage("pre_tool", time.time() - _t0)

    return None  # Allow execution


# ---------------------------------------------------------------------------
# Hook: post_tool_call — THE RUNTIME HOT PATH ENTRY POINT
# ---------------------------------------------------------------------------


def post_tool_call(
    tool_name: str = "",
    args: Optional[dict] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Called after every Hermes tool invocation.

    Standard tools (terminal, read_file, etc.) flow through:
        1. RuntimeHotPath: Telemetry record
        2. RuntimeHotPath: Learning loop (Reflection → Experience → Memory)
        3. RuntimeHotPath: DriftAnalyzer (async, 10% sampling)

    core_* tools skip this hook — their pipeline already ran in execute_tool().
    """
    if not tool_name:
        return

    # core_* tools already ran the full pipeline inside execute_tool()
    if tool_name.startswith("core_"):
        return

    # ---- 1. RuntimeHotPath post-execution pipeline ----
    _ensure_runtime()
    if _runtime is None:
        return

    try:
        # Detect success/failure from result
        _t0_hook = time.time()
        success = True
        error = ""
        output = ""

        if isinstance(result, str):
            output = result
        elif result is None:
            output = ""
        else:
            try:
                # Truncate before serializing to avoid huge JSON dumps
                raw = str(result)
                output = raw[:500]
            except (TypeError, ValueError):
                output = str(result)

        # Detect failure signals in output
        _output_lower = output[:500].lower() if output else ""
        if any(kw in _output_lower for kw in [
            "traceback", "error:", "exception:", "failed", "permission denied",
            "not found", "no such file", "connection refused", "timed out",
            "exit code 1", "exit_code=1",
        ]):
            success = False
            error = output[:200]

        # Compute duration from pre_tool_call timer
        _start = _call_timers.pop(tool_call_id or tool_name, None)
        _duration = (time.time() - _start) if _start else 0.0

        # ── Stage timing: memory context retrieval ──
        _t0 = time.time()

        # 2a. Retrieve memory context for learning loop
        memory_ctx = {}
        try:
            if hasattr(_runtime, "_retrieve_memory_context"):
                memory_ctx = _runtime._retrieve_memory_context(
                    tool_name, args or {}, {"goal": tool_name, "domain": "general"},
                )
        except Exception as exc:
            logger.debug("hermes-core: memory context retrieval: %s", exc)

        _profile_stage("memory_ctx", time.time() - _t0)

        # ── Stage timing: learning loop ──
        _t0 = time.time()

        # 2b. Learning loop: Reflection → Experience → Memory (with PolicyEngine gate)
        try:
            if hasattr(_runtime, "_run_learning_loop"):
                _policy_allowed = True
                if _runtime._policy is not None:
                    try:
                        _policy_result = _runtime._policy.check_action(
                            tool_name, args or {}
                        )
                        _policy_allowed = _policy_result.get("allowed", True)
                    except Exception as exc:
                        logger.debug(
                            "hermes-core: policy check before learning: %s", exc
                        )

                if not _policy_allowed:
                    logger.debug(
                        "hermes-core: learning skipped — policy denied for %s",
                        tool_name,
                    )
                else:
                    _runtime._run_learning_loop(
                    tool_name=tool_name,
                    task_id=task_id or tool_call_id or f"auto_{int(time.time())}",
                    success=success,
                    output=output[:1000],
                    error=error,
                    duration=_duration,
                    ctx={"goal": tool_name, "domain": "general"},
                    memory_context=memory_ctx,
                )
        except Exception as exc:
            logger.debug("hermes-core: learning loop: %s", exc)

        _profile_stage("learning_loop", time.time() - _t0)

        # ── Stage timing: TaskGraph ──
        _t0 = time.time()

        # 2c. TaskGraph — record tool call as task node (lazy graph creation)
        try:
            if _runtime._task_graph is not None and hasattr(_runtime._task_graph, "add_node"):
                import uuid as _uuid
                from datetime import datetime, timezone
                _now = datetime.now(timezone.utc).isoformat()
                _node_id = f"tc_{tool_call_id or _uuid.uuid4().hex[:8]}"
                _tg_mod = sys.modules.get("task_graph")
                if _tg_mod is not None:
                    TaskNode = _tg_mod.TaskNode
                    _node = TaskNode(
                        node_id=_node_id,
                        action=tool_name,
                        params={"args_keys": list((args or {}).keys())},
                        depends_on=[],
                        status="completed" if success else "failed",
                        result={"output_len": len(output)} if success else None,
                        error=error[:200] if error else None,
                        started_at=_now,
                        completed_at=_now,
                    )
                    if not hasattr(_runtime, "_session_graph_id"):
                        _runtime._session_graph_id = None
                    if _runtime._session_graph_id is None:
                        _graph_id = f"session_{session_id or 'default'}"
                        try:
                            _runtime._session_graph_id = _runtime._task_graph.create_graph(
                                name=_graph_id, nodes=[_node],
                            )
                        except Exception as exc:
                            logger.debug("hermes-core: TaskGraph create_graph: %s", exc)
                    else:
                        try:
                            _runtime._task_graph.add_node(_runtime._session_graph_id, _node)
                        except Exception as exc:
                            logger.debug("hermes-core: TaskGraph add_node: %s", exc)
        except Exception as exc:
            logger.debug("hermes-core: TaskGraph record: %s", exc)

        _profile_stage("task_graph", time.time() - _t0)

        # ── Stage timing: EventBus ──
        _t0 = time.time()

        # 2d. EventBus publish — tool.completed / tool.failed
        try:
            _pub_bus = None
            _eb_mod = sys.modules.get("event_bus")
            if _eb_mod is not None and hasattr(_eb_mod, "get_bus"):
                _pub_bus = _eb_mod.get_bus()
            if _pub_bus is not None and hasattr(_pub_bus, "publish"):
                _pub_bus.publish(
                    "tool.completed" if success else "tool.failed",
                    {
                        "tool": tool_name,
                        "task_id": task_id or tool_call_id,
                        "duration_s": round(_duration, 3),
                        "success": success,
                        "error": error[:200] if error else "",
                    },
                )
        except Exception as exc:
            logger.debug("hermes-core: EventBus publish: %s", exc)

        _profile_stage("event_bus", time.time() - _t0)

        # 2e. Drift analysis — async (10% sampling, daemon thread)
        if random.random() < 0.1:
            threading.Thread(
                target=_async_drift_analysis,
                args=(_runtime,),
                daemon=True,
            ).start()

        # 2f. Self-observation — async (5% sampling, daemon thread)
        if random.random() < 0.05:
            threading.Thread(
                target=_async_self_observation,
                args=(_runtime,),
                daemon=True,
            ).start()

        # 2g. Watchdog heartbeat — every tool call
        try:
            _wd_mod = sys.modules.get("watchdog")
            if _wd_mod is not None and hasattr(_wd_mod, "get_watchdog"):
                _wd = _wd_mod.get_watchdog()
                if _wd is not None and hasattr(_wd, "heartbeat"):
                    _wd.heartbeat(tool_name)
        except Exception as exc:
            logger.debug("hermes-core: watchdog heartbeat: %s", exc)

        # 2h. Goal auto-tracking — register new goal per unique tool call
        try:
            _gm_mod = sys.modules.get("goal_manager")
            if _gm_mod is not None and hasattr(_gm_mod, "get_goal_manager"):
                _gm = _gm_mod.get_goal_manager()
                if _gm is not None and hasattr(_gm, "register_goal"):
                    # Register a goal for each unique tool call (exact match)
                    if tool_name not in _GOAL_TOOL_INDEX:
                        goal_id = _gm.register_goal(
                            f"Execute tool: {tool_name}",
                            priority=3,
                            context={"domain": "tool"},
                        )
                        if goal_id:
                            _GOAL_TOOL_INDEX[tool_name] = goal_id

                    # Complete on success — O(1) lookup via reverse index
                    if success and hasattr(_gm, "complete_goal"):
                        gid = _GOAL_TOOL_INDEX.get(tool_name)
                        if gid is not None:
                            _gm.complete_goal(gid)
        except Exception as exc:
            logger.debug("hermes-core: goal tracking: %s", exc)

    except Exception as exc:
        logger.debug("hermes-core: post_tool_call runtime pipeline error: %s", exc)

    # Record total hook time (includes: output parsing + memory_ctx + learning_loop + task_graph + event_bus)
    _profile_stage("_total_hook", time.time() - _t0_hook)

    # ── Periodic latency profile summary ──
    global _profile_call_count
    _profile_call_count += 1
    if _profile_call_count >= _profile_log_interval:
        _log_profile_summary()


# ---------------------------------------------------------------------------
# Async helpers — run in daemon thread to avoid blocking the tool pipeline
# ---------------------------------------------------------------------------


def _async_drift_analysis(runtime) -> None:
    """Run drift analysis in a background thread.

    If drift is detected, applies active defense AND feeds back to
    PolicyEngine so future ``check_action()`` calls reflect the
    adjusted thresholds.
    """
    try:
        if hasattr(runtime, "_drift") and runtime._drift is not None:
            if hasattr(runtime._drift, "get_summary"):
                drift = runtime._drift.get_summary()
            elif hasattr(runtime._drift, "analyze_all"):
                drift = runtime._drift.analyze_all()
            else:
                drift = {}
            if drift and drift.get("action_required"):
                # 1. Active defense (memory pruning, planner reset, etc.)
                if hasattr(runtime, "_apply_active_defense"):
                    runtime._apply_active_defense(drift)

                # 2. PolicyEngine feedback
                _apply_drift_to_policy(runtime, drift)
    except Exception as exc:
        logger.debug("hermes-core: async drift analysis: %s", exc)


def _apply_drift_to_policy(runtime, drift: dict) -> None:
    """Translate drift suggestions into PolicyEngine ``update_policy`` calls."""
    if not hasattr(runtime, "_policy") or runtime._policy is None:
        return
    if not hasattr(runtime._policy, "update_policy"):
        return
    suggested = drift.get("suggested_actions", [])
    if not suggested:
        return
    for suggestion in suggested:
        try:
            action_part = suggestion.split(":", 1)[0].strip()
            if action_part == "raise_threshold":
                runtime._policy.update_policy("raise_threshold",
                                              adjust={"new_risk": "high"})
                logger.info("hermes-core: drift → policy: raised risk threshold to 'high'")
            elif action_part == "lower_threshold":
                runtime._policy.update_policy("lower_threshold",
                                              adjust={"new_risk": "medium"})
                logger.info("hermes-core: drift → policy: lowered risk threshold to 'medium'")
            elif action_part == "throttle_eventbus":
                runtime._policy.update_policy("tune_limit",
                                              adjust={"limit": "max_requests_per_domain",
                                                      "value": 10})
                logger.info("hermes-core: drift → policy: throttled max_requests_per_domain to 10")
            elif action_part == "reduce_concurrency":
                runtime._policy.update_policy("tune_limit",
                                              adjust={"limit": "max_concurrent_tasks",
                                                      "value": 1})
                logger.info("hermes-core: drift → policy: reduced concurrency to 1")
        except Exception as exc:
            logger.debug("hermes-core: drift→policy translate: %s", exc)


def _async_self_observation(runtime) -> None:
    """Run self-observation in a background thread."""
    try:
        if runtime._self_obs is not None and hasattr(runtime._self_obs, "run_once"):
            report = runtime._self_obs.run_once()
            if report and hasattr(report, "alerts") and report.alerts:
                logger.warning(
                    "hermes-core: self-obs alerts: %s",
                    "; ".join(report.alerts[:3]),
                )
    except Exception as exc:
        logger.debug("hermes-core: async self-observation: %s", exc)


# ---------------------------------------------------------------------------
# Hook: post_llm_call
# ---------------------------------------------------------------------------


def post_llm_call(
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: float = 0.0,
    session_id: str = "",
    **_: Any,
) -> None:
    """Called after every LLM API call.

    Records token usage for telemetry via RuntimeHotPath.
    """
    if not model:
        return

    # Pass latency to RuntimeHotPath telemetry
    _ensure_runtime()
    if _runtime is None or _runtime._telemetry is None:
        return

    try:
        if hasattr(_runtime._telemetry, "record_llm_latency"):
            _runtime._telemetry.record_llm_latency(duration_ms / 1000.0)
    except Exception as exc:
        logger.debug("hermes-core: Telemetry record_llm_latency: %s", exc)


# ---------------------------------------------------------------------------
# Hook: on_session_end
# ---------------------------------------------------------------------------


def on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Called when a Hermes session ends.

    Generates a session summary with runtime telemetry data, flushes
    event logs, and emits a session telemetry report.
    """
    global _session_start_time

    duration = time.time() - _session_start_time if _session_start_time else 0.0
    _ensure_runtime()

    # Build session telemetry summary
    runtime_telemetry = {}
    if _runtime is not None:
        try:
            runtime_telemetry = _runtime.get_runtime_telemetry()
        except Exception as exc:
            logger.debug("hermes-core: runtime telemetry snapshot: %s", exc)

    stability_score = runtime_telemetry.get("current_stability", 0.0)
    behavior_mode = runtime_telemetry.get("behavior_mode", "unknown")

    # Stop background threads to prevent leaks
    if _runtime is not None:
        for attr_name in ("_self_obs", "_watchdog", "_supervisor"):
            try:
                subsystem = getattr(_runtime, attr_name, None)
                if subsystem is not None and hasattr(subsystem, "stop"):
                    subsystem.stop()
                    logger.debug("hermes-core: stopped %s", attr_name)
            except Exception as exc:
                logger.debug("hermes-core: stop %s: %s", attr_name, exc)

    logger.info(
        "hermes-core: session %s ended (%.1fs, stability=%.2f, mode=%s)",
        session_id,
        duration,
        stability_score,
        behavior_mode,
    )
