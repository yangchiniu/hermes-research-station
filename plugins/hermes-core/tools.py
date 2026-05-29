"""hermes-core plugin tools — Runtime Hot Path tool handlers.

Each handler runs through the RuntimeHotPath.execute_tool() pipeline:

    PolicyEngine → WorldModel → RuntimeSupervisor → ToolRegistry
    → EXECUTE
    → Telemetry → Reflection → Experience → Memory → DriftAnalyzer

All imports are try/except — graceful degradation if core modules missing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy RuntimeHotPath reference
# ---------------------------------------------------------------------------

_runtime = None


def _get_runtime():
    global _runtime
    if _runtime is None:
        try:
            from .runtime_integration import get_runtime
            _runtime = get_runtime()
        except Exception as exc:
            logger.debug("hermes-core: runtime load failed: %s", exc)
    return _runtime


# ---------------------------------------------------------------------------
# Legacy standalone module accessors (fallback if runtime unavailable)
# ---------------------------------------------------------------------------

_core_modules: dict[str, Any] = {}


def _import(name: str) -> Any:
    """Import and cache a core module via _lazy_core (unified lazy import)."""
    if name not in _core_modules:
        try:
            from .__init__ import _lazy_core as _lc
            _core_modules[name] = _lc(name)
        except Exception as exc:
            logger.debug("hermes-core: import %s failed: %s", name, exc)
            _core_modules[name] = None
    return _core_modules[name]


def _get_kernel():
    m = _import("kernel")
    if m is not None:
        if hasattr(m, "Kernel"):
            return m.Kernel()
        if hasattr(m, "get_kernel"):
            return m.get_kernel()
    return None


def _get_telemetry():
    m = _import("telemetry")
    if m is not None and hasattr(m, "Telemetry"):
        return m.Telemetry()
    return None


# ---------------------------------------------------------------------------
# ── TOOL HANDLERS ──
# Each handler delegates to RuntimeHotPath.execute_tool() which runs the
# full Cognitive pipeline.  If RuntimeHotPath is unavailable, falls back
# to direct module calls (legacy behaviour).
# ---------------------------------------------------------------------------


# =========================================================================
# TOOL: core_plan
# =========================================================================


def _handle_plan_direct(goal: str, context: str = "") -> str:
    """Direct planner call (fallback when RuntimeHotPath unavailable)."""
    planner = _import("planner")
    if planner is None:
        return "Planner module not available."

    try:
        p = planner.Planner() if hasattr(planner, "Planner") else None
        if p is None and hasattr(planner, "get_planner"):
            p = planner.get_planner()
        if p is None:
            return "Planner instance not creatable."

        if hasattr(p, "plan"):
            result = p.plan(goal, context={"context_str": context} if context else {})
        else:
            return "Planner has no 'plan' method."

        if isinstance(result, dict):
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return str(result)
    except Exception as exc:
        return f"Planner error: {exc}"


def handle_core_plan(params: dict | None = None, **kwargs) -> str:
    """Decompose a goal into a structured plan via Planner.

    Parameters
    ----------
    params : dict
        Supported keys:

        * ``goal`` (str, required) — what to plan for
        * ``context`` (str, optional) — additional context
        * ``auto_execute`` (bool, optional, default False) — if True, execute
          the plan steps through the runtime and return the execution log
          instead of the raw plan JSON.

    Returns
    -------
    str
        Plan JSON (default) or execution report (when ``auto_execute=True``).
    """
    goal = params.get("goal", "") if isinstance(params, dict) else ""
    if not goal:
        return "Please provide a 'goal' parameter describing what you want to plan for."
    context = params.get("context", "") if isinstance(params, dict) else ""
    auto_execute = params.get("auto_execute", False) if isinstance(params, dict) else False

    rt = _get_runtime()
    plan_dict = None

    if rt is not None:
        # Try Planner singleton first
        try:
            from .__init__ import _lazy_core as _lc

            _planner_mod = _lc("planner")
            p = _planner_mod.get_planner() if _planner_mod and hasattr(_planner_mod, "get_planner") else None
            if p is not None and hasattr(p, "plan"):
                from runtime_integration import execute_tool

                result = execute_tool(
                    tool_name="planner",
                    args={"goal": goal, "context_str": context},
                    handler=lambda **a: json.dumps(
                        p.plan(
                            a.get("goal", goal),
                            context={"context_str": a.get("context_str", context)},
                        ).to_dict(),
                        indent=2,
                        ensure_ascii=False,
                        default=str,
                    ),
                    context={"goal": goal, "domain": "planning"},
                )
                plan_json = result.output
                plan_dict = json.loads(plan_json) if plan_json else None

                if auto_execute and plan_dict is not None:
                    # Execute the plan and return execution log
                    try:
                        from plan_executor import execute_plan
                        exec_result = execute_plan(plan_dict, rt, auto_execute=True)
                        return json.dumps(exec_result, indent=2, ensure_ascii=False, default=str)
                    except Exception as exec_exc:
                        return f"Plan generated but execution failed: {exec_exc}\n\nRaw plan:\n{plan_json}"

                return f"[Runtime Hot Path: stability={result.stability_after:.2f}, mode={result.behavior_controls.get('mode', '?')}]\n{plan_json}"
        except Exception:
            pass

    # Fallback to direct
    if auto_execute:
        # Can't auto-execute without runtime — fall back to plan only
        plan_only = _handle_plan_direct(goal, context)
        return f"Runtime not available for auto_execute. Plan only:\n{plan_only}"
    return _handle_plan_direct(goal, context)


# =========================================================================
# TOOL: core_ooda
# =========================================================================


def _handle_ooda_direct(goal: str, context: str = "") -> str:
    """Direct OODA call (fallback)."""
    ooda = _import("ooda_loop")
    if ooda is None:
        return "OODA Loop module not available."

    try:
        loop = ooda.OODALoop() if hasattr(ooda, "OODALoop") else None
        if loop is None and hasattr(ooda, "get_ooda"):
            loop = ooda.get_ooda()
        if loop is None:
            return "OODA Loop instance not creatable."

        if hasattr(loop, "run"):
            result = loop.run(goal, context=context)
        elif hasattr(loop, "cycle"):
            result = loop.cycle(goal)
        else:
            return "OODA has no 'run' or 'cycle' method."

        if isinstance(result, dict):
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return str(result)
    except Exception as exc:
        return f"OODA error: {exc}"


def handle_core_ooda(params: dict | None = None, **kwargs) -> str:
    """Execute an OODA (Observe-Orient-Decide-Act) cycle."""
    goal = params.get("goal", "") if isinstance(params, dict) else ""
    context = params.get("context", "") if isinstance(params, dict) else ""
    rt = _get_runtime()
    if rt is not None:
        try:
            from .__init__ import _lazy_core as _lc

            _ooda_mod = _lc("ooda_loop")
            loop = _ooda_mod.get_ooda() if _ooda_mod and hasattr(_ooda_mod, "get_ooda") else None
            if loop is not None and hasattr(loop, "run"):
                from runtime_integration import execute_tool

                result = execute_tool(
                    tool_name="ooda",
                    args={"goal": goal, "context": context},
                    handler=lambda **a: json.dumps(
                        _ooda_mod.run(a.get("goal", goal)),
                        indent=2,
                        ensure_ascii=False,
                        default=str,
                    ),
                    context={"goal": goal, "domain": "decision"},
                )
                return f"[Runtime Hot Path: stability={result.stability_after:.2f}, mode={result.behavior_controls.get('mode', '?')}]\n{result.output}"
        except Exception:
            pass

    return _handle_ooda_direct(goal, context)


# =========================================================================
# TOOL: core_kernel_exec
# =========================================================================


def handle_core_kernel(
    params: dict | None = None,
    **kwargs,
) -> str:
    """Interact with the Hermes Core Kernel via RuntimeHotPath."""
    action = params.get("action", "").lower() if isinstance(params, dict) else ""
    if not action:
        return "Please provide an 'action' parameter: 'status', 'list', 'start', or 'stop'."
    task_id = params.get("task_id", "") if isinstance(params, dict) else ""
    description = params.get("description", "") if isinstance(params, dict) else ""
    rt = _get_runtime()
    kernel = _get_kernel()

    # Status / list — read-only, no pipeline needed
    if action == "status":
        if rt is not None:
            status = rt.get_status()
        elif kernel is not None and hasattr(kernel, "get_status"):
            status = kernel.get_status()
        else:
            return "Kernel not available."
        return json.dumps(status, indent=2, ensure_ascii=False, default=str)

    if action == "list":
        if kernel is not None and hasattr(kernel, "list_tasks"):
            tasks = kernel.list_tasks()
            return json.dumps(tasks, indent=2, ensure_ascii=False, default=str)
        return "Kernel has no 'list_tasks' method"

    # Start / stop — use direct kernel calls (not tool execution)
    if action == "start":
        tid = task_id or f"task-{int(time.time())}"
        if kernel is not None and hasattr(kernel, "start_task"):
            result = kernel.start_task(tid, description=description)
            return (
                json.dumps(result, indent=2, ensure_ascii=False, default=str)
                if isinstance(result, dict)
                else str(result)
            )
        return f"Kernel has no 'start_task' method (task_id={tid})"

    if action == "stop":
        if not task_id:
            return "stop requires a task_id"
        if kernel is not None and hasattr(kernel, "stop_task"):
            result = kernel.stop_task(task_id)
            return str(result) if result is not None else f"Task {task_id} stopped"
        return f"Kernel has no 'stop_task' method"

    return f"Unknown kernel action: {action}"


# =========================================================================
# TOOL: core_field_test
# =========================================================================


def handle_core_field_test(params: dict | None = None, **kwargs) -> str:
    """Run a field test through the RuntimeHotPath."""
    hours = float(params.get("hours", 1.0)) if isinstance(params, dict) else 1.0
    simulate = params.get("simulate", True) if isinstance(params, dict) else True
    fr_module = _import("field_runner")
    if fr_module is None:
        return "Field Runner module not available."

    try:
        if simulate:
            if hasattr(fr_module, "simulate"):
                result = fr_module.simulate(hours=hours)
            else:
                return "Field Runner loaded but has no 'simulate' function"
        else:
            runner_cls = getattr(fr_module, "FieldRunner", None)
            if runner_cls is not None:
                runner = runner_cls()
                result = runner.run(hours=hours)
            elif hasattr(fr_module, "run_field_test"):
                result = fr_module.run_field_test(int(hours))
            else:
                return "Field Runner has no usable run method"

        if isinstance(result, dict):
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return str(result)
    except Exception as exc:
        return f"Field test error: {exc}"


# =========================================================================
# TOOL: core_health
# =========================================================================


def handle_core_health(params: dict | None = None, **kwargs) -> str:
    """Show telemetry health + runtime status."""
    rt = _get_runtime()
    tm = _get_telemetry()

    lines = ["=== Hermes Core Runtime Status ==="]

    # Runtime Hot Path status
    if rt is not None:
        try:
            status = rt.get_status()
            lines.append(f"  Runtime Hot Path: ACTIVE")
            lines.append(f"  Stability: {status.get('stability_score', 0):.2%}")
            lines.append(f"  Behavior Mode: {status.get('behavior_mode', '?')}")
            lines.append(
                f"  Subsystems: {sum(1 for v in status.get('subsystems', {}).values() if v)}"
                f"/{len(status.get('subsystems', {}))}"
            )

            tc = status.get("behavior_controls", {})
            lines.append(
                f"  Controls: throttle={tc.get('throttle', False)}, "
                f"safe={tc.get('safe_mode', False)}, "
                f"freeze={tc.get('freeze', False)}"
            )

            prefs = status.get("planner_tool_preferences", {})
            if prefs:
                top = sorted(prefs.items(), key=lambda x: -x[1])[:5]
                lines.append(f"  Top tool preferences: {', '.join(f'{k}={v:.2f}' for k, v in top)}")
        except Exception as exc:
            lines.append(f"  Runtime Hot Path: error ({exc})")
    else:
        lines.append("  Runtime Hot Path: INACTIVE (using fallback)")

    # Telemetry data
    if tm is not None:
        try:
            latest = tm.get_latest() if hasattr(tm, "get_latest") else None
            if latest is not None:
                score = tm.get_health_score() if hasattr(tm, "get_health_score") else None
                if score is not None:
                    lines.append(f"  Health: {score:.2%} composite")
                    lines.insert(1, f"  Health: {score:.2%} composite")
                lines.append(f"  CPU load: {latest.cpu_load:.1%}")
                lines.append(f"  RAM: {latest.ram_percent:.1f}%")
                lines.append(f"  Disk: {latest.disk_percent:.1f}%")
                lines.append(f"  Threads: {latest.thread_count}")
                lines.append(f"  Goals: {latest.active_goals}")
            else:
                lines.append("  Telemetry: no data yet")
        except Exception as exc:
            lines.append(f"  Telemetry error: {exc}")
    else:
        lines.append("  Telemetry module: unavailable")

    return "\n".join(lines)


# =========================================================================
# TOOL: core_experience
# =========================================================================


def _get_experience_manager():
    m = _import("experience_manager")
    if m is not None and hasattr(m, "get_experience"):
        try:
            return m.get_experience()
        except Exception:
            pass
    if m is not None and hasattr(m, "ExperienceManager"):
        try:
            return m.ExperienceManager()
        except Exception:
            pass
    return None


def handle_core_experience(params: dict | None = None, **kwargs) -> str:
    """Query the experience manager through RuntimeHotPath."""
    query = params.get("query", "") if isinstance(params, dict) else ""
    limit = int(params.get("limit", 10)) if isinstance(params, dict) else 10
    rt = _get_runtime()
    em = _get_experience_manager()

    # If runtime is active, use it for context-aware retrieval
    if rt is not None and em is not None:
        try:
            output = {"_runtime_active": True}

            # Use actual ExperienceManager APIs
            if hasattr(em, "get_summary"):
                output["summary"] = em.get_summary()
            if query and hasattr(em, "get_strategies"):
                output["strategies"] = em.get_strategies(domain=query, min_success_rate=0.1)
            if query and hasattr(em, "get_known_failures"):
                output["known_failures"] = em.get_known_failures(domain=query)
            if hasattr(em, "get_tool_stats"):
                output["tool_stats"] = em.get_tool_stats()

            # Also get planner preferences through runtime
            status = rt.get_status()
            prefs = status.get("planner_tool_preferences", {})
            output["_planner_tool_preferences"] = prefs
            output["_stability"] = status.get("stability_score", 0)
            output["_mode"] = status.get("behavior_mode", "")

            return json.dumps(output, indent=2, ensure_ascii=False, default=str)
        except Exception as exc:
            return f"Experience Manager error: {exc}"

    # Fallback: direct calls
    if em is None:
        return "Experience Manager module not available."

    try:
        output = {}
        if hasattr(em, "get_summary"):
            output["summary"] = em.get_summary()
        if query and hasattr(em, "get_strategies"):
            output["strategies"] = em.get_strategies(domain=query, min_success_rate=0.1)
        if query and hasattr(em, "get_known_failures"):
            output["known_failures"] = em.get_known_failures(domain=query)
        if hasattr(em, "get_tool_stats"):
            output["tool_stats"] = em.get_tool_stats()

        if not output:
            return "Experience Manager has no usable query methods."
        return json.dumps(output, indent=2, ensure_ascii=False, default=str)
    except Exception as exc:
        return f"Experience Manager error: {exc}"


# ---------------------------------------------------------------------------
# ── SLASH COMMANDS ──
# ---------------------------------------------------------------------------


def slash_field_test(raw_args: str) -> str:
    """/field-test [hours] — run a field test simulation."""
    try:
        hours = float(raw_args.strip()) if raw_args.strip() else 1.0
    except ValueError:
        return "Usage: /field-test [hours]  (e.g. /field-test 2)"

    return handle_core_field_test(hours=hours, simulate=True)


def slash_health(raw_args: str) -> str:
    """/health — show current runtime health status."""
    return handle_core_health()
