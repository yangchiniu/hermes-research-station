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
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level lazy state
# ---------------------------------------------------------------------------

_runtime = None
_event_logger = None
_telemetry = None
_telemetry_started = False
_session_start_time: float = 0.0
_call_timers: dict = {}  # tool_call_id → start_time for duration tracking


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


def _ensure_legacy() -> None:
    """Legacy init for event_logger (backward compat)."""
    global _event_logger, _telemetry, _telemetry_started

    if _event_logger is None:
        try:
            from . import _core_on_path, _core_off_path

            _core_on_path()
            import event_logger as _el
            import telemetry as _tm

            _core_off_path()
            _event_logger = _el.get_logger()
            _telemetry = _tm.Telemetry()
            logger.debug("hermes-core: legacy init ok")
        except Exception as exc:
            logger.warning("hermes-core: legacy init failed: %s", exc)


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

    Initialises the RuntimeHotPath, EventBus connectivity, legacy event
    logger, telemetry collection, and registers EventBus listeners.
    """
    global _session_start_time, _telemetry_started
    _session_start_time = time.time()

    # Init both new runtime and legacy systems
    _ensure_runtime()
    _ensure_legacy()

    # Log session start
    if _event_logger is not None:
        try:
            _event_logger.log(
                "session.start",
                {"session_id": session_id},
                severity="info",
            )
        except Exception:
            pass

    # Start telemetry background collection
    if _telemetry is not None and not _telemetry_started:
        try:
            _telemetry.start(interval_s=60.0)
            _telemetry_started = True
            logger.debug("hermes-core: telemetry started")
        except Exception:
            pass

    # --- NEW: Register EventBus listeners for cross-module signalling ---
    try:
        if _runtime:
            from . import _core_on_path, _core_off_path

            _core_on_path()
            try:
                import event_bus

                bus = event_bus.get_event_bus()
                if bus and hasattr(bus, "subscribe"):
                    bus.subscribe("tool.completed", _on_tool_completed_event)
                    bus.subscribe("tool.failed", _on_tool_failed_event)
                    bus.subscribe("recovery.triggered", _on_recovery_event)
                    logger.info("hermes-core: registered EventBus listeners")
            except Exception:
                pass
            finally:
                _core_off_path()
    except Exception:
        pass

    logger.info("hermes-core: session %s started", session_id)


# ---------------------------------------------------------------------------
# EventBus event handlers
# ---------------------------------------------------------------------------


def _on_tool_completed_event(event_data: dict) -> None:
    """Called when a tool.completed event fires on EventBus."""
    pass  # Reserved for future proactive behaviours


def _on_tool_failed_event(event_data: dict) -> None:
    """Called when a tool.failed event fires."""
    _ensure_runtime()
    if _runtime is None:
        return

    # If stability is low after a failure, trigger observation
    try:
        status = _runtime.get_status()
        if status.get("behavior_mode") in ("safe", "frozen"):
            # In safe/frozen mode, log a warning
            logger.warning(
                "hermes-core: tool failure in %s mode",
                status["behavior_mode"],
            )
            if _event_logger is not None:
                _event_logger.log(
                    "runtime.behavior_warning",
                    {
                        "mode": status["behavior_mode"],
                        "stability": status.get("stability_score", 0),
                        "event": event_data,
                    },
                    severity="warning",
                )
    except Exception:
        pass


def _on_recovery_event(event_data: dict) -> None:
    """Called when recovery.triggered event fires."""
    logger.info("hermes-core: recovery triggered via EventBus")
    # Future: trigger self-observation loop


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
    except Exception:
        pass

    # ---- 2. WorldModel snapshot (async, best-effort) ----
    try:
        if _runtime._world_model is not None:
            _runtime._world_model.get_world_state(refresh=True)
    except Exception:
        pass

    # ---- 3. Telemetry record_attempt ----
    try:
        if _telemetry is not None:
            if hasattr(_telemetry, "record_tool_attempt"):
                _telemetry.record_tool_attempt(tool_name=tool_name)
            elif hasattr(_telemetry, "collect"):
                _telemetry.collect()
    except Exception:
        pass

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

    This is the Runtime Hot Path entry point. Every tool call flows through:
        1. Legacy: log to event_logger (backward compat)
        2. RuntimeHotPath: Telemetry record
        3. RuntimeHotPath: Learning loop (Reflection → Experience → Memory)
        4. RuntimeHotPath: DriftAnalyzer observe
        5. RuntimeHotPath: Stability → Behavior Control adjustment
    """
    if not tool_name:
        return

    # ---- 1. Legacy logging ----
    _ensure_legacy()

    if _event_logger is not None:
        try:
            safe_args = {}
            if isinstance(args, dict):
                for k, v in args.items():
                    if isinstance(v, str) and len(v) > 500:
                        safe_args[k] = v[:100] + "..."
                    else:
                        safe_args[k] = v

            status = "ok"
            if result is None:
                status = "empty"
            elif isinstance(result, str) and len(result) >= 200_000:
                status = "truncated"

            _event_logger.log(
                "tool.call",
                {
                    "tool": tool_name,
                    "args": safe_args,
                    "status": status,
                    "task_id": task_id,
                    "session_id": session_id,
                    "tool_call_id": tool_call_id,
                },
                severity="info",
            )
        except Exception:
            pass

    # ---- 2. RuntimeHotPath post-execution pipeline ----
    _ensure_runtime()
    if _runtime is None:
        return

    try:
        # Detect success/failure from result
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

        # 2a. Telemetry record
        try:
            if _telemetry is not None:
                if hasattr(_telemetry, "record_tool"):
                    _telemetry.record_tool(tool_name=tool_name, success=success)
                elif hasattr(_telemetry, "collect"):
                    _telemetry.collect()
        except Exception:
            pass

        # 2b. Learning loop: Reflection → Experience → Memory
        try:
            # Use RuntimeHotPath's learning loop
            if hasattr(_runtime, "_run_learning_loop"):
                _runtime._run_learning_loop(
                    tool_name=tool_name,
                    task_id=task_id or tool_call_id or f"auto_{int(time.time())}",
                    success=success,
                    output=output[:300],
                    error=error,
                    duration=_duration,
                    ctx={"goal": tool_name, "domain": "general"},
                )
        except Exception:
            pass

        # 2c. Drift analysis (periodic, not every call — sample rate ~10%)
        try:
            if hash(tool_call_id or tool_name) % 10 == 0:  # 10% sampling
                if hasattr(_runtime, "_drift") and _runtime._drift is not None:
                    if hasattr(_runtime._drift, "observe"):
                        drift = _runtime._drift.observe({
                            "tool": tool_name,
                            "success": success,
                            "task_id": task_id or "",
                        })
                        if drift and drift.get("action_required"):
                            _runtime._apply_active_defense(drift)
        except Exception:
            pass

    except Exception as exc:
        logger.debug("hermes-core: post_tool_call runtime pipeline error: %s", exc)


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

    Records token usage and latency for telemetry.
    """
    if not model:
        return

    _ensure_legacy()

    # Log to event_logger
    if _event_logger is not None:
        try:
            _event_logger.log(
                "llm.call",
                {
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "duration_ms": duration_ms,
                    "session_id": session_id,
                },
                severity="info",
            )
        except Exception:
            pass

    # Pass latency to telemetry
    if _telemetry is not None and duration_ms > 0:
        try:
            if hasattr(_telemetry, "record_llm_latency"):
                _telemetry.record_llm_latency(duration_ms / 1000.0)
        except Exception:
            pass


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
    _ensure_legacy()
    _ensure_runtime()

    # Build session telemetry summary
    runtime_telemetry = {}
    if _runtime is not None:
        try:
            runtime_telemetry = _runtime.get_runtime_telemetry()
        except Exception:
            pass

    stability_score = runtime_telemetry.get("current_stability", 0.0)
    behavior_mode = runtime_telemetry.get("behavior_mode", "unknown")

    # Log session end
    if _event_logger is not None:
        try:
            _event_logger.log(
                "session.end",
                {
                    "session_id": session_id,
                    "duration_s": round(duration, 1),
                    "completed": completed,
                    "interrupted": interrupted,
                    "stability_score": round(stability_score, 3),
                    "behavior_mode": behavior_mode,
                    "total_tasks": runtime_telemetry.get("total_tasks_executed", 0),
                    "subsystems_available": runtime_telemetry.get(
                        "subsystems_available", 0
                    ),
                },
                severity="info",
            )
        except Exception:
            pass

    # Collect final telemetry snapshot
    if _telemetry is not None:
        try:
            _telemetry.collect()
        except Exception:
            pass

    logger.info(
        "hermes-core: session %s ended (%.1fs, stability=%.2f, mode=%s)",
        session_id,
        duration,
        stability_score,
        behavior_mode,
    )
