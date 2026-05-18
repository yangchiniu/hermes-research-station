"""runtime_integration.py — Runtime Hot Path bridge for Hermes Core.

Connects the agent's tool execution loop with the Cognitive Infrastructure:

    Tool Call
    → PolicyEngine
    → WorldModel Validation
    → RuntimeSupervisor Budget Check
    → ToolRegistry Risk Check
    → Execute
    → Telemetry Record
    → ReflectionEngine
    → ExperienceManager Update
    → MemoryManager Store
    → DriftAnalyzer Observe
    → Stability → Behavior Control

This is the ONLY bridge between the agent runtime and the core cognitive layer.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_HERMES_HOME = os.path.expanduser("~/.hermes")
_CORE_DIR = os.path.join(_HERMES_HOME, "core")

if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

# ---------------------------------------------------------------------------
# Lazy core module accessors (all try/except — graceful degradation)
# ---------------------------------------------------------------------------

_core_modules: Dict[str, Any] = {}
_core_lock = threading.Lock()


def _lazy(name: str) -> Any:
    if name not in _core_modules:
        with _core_lock:
            if name not in _core_modules:
                added = False
                try:
                    if _CORE_DIR not in sys.path:
                        sys.path.insert(0, _CORE_DIR)
                        added = True
                    _core_modules[name] = __import__(name)
                except Exception as exc:
                    # Check if already in sys.modules (imported as side effect)
                    if name in sys.modules:
                        _core_modules[name] = sys.modules[name]
                    else:
                        logger.debug("runtime: import %s failed: %s", name, exc)
                        _core_modules[name] = None
                finally:
                    if added and _CORE_DIR in sys.path:
                        sys.path.remove(_CORE_DIR)
    return _core_modules[name]


def _get_kernel():
    m = _lazy("kernel")
    if m and hasattr(m, "get_kernel"):
        try:
            return m.get_kernel()
        except Exception as exc:
            logger.debug("runtime: _get_kernel: Check if already in sys.modules (imported as si: %s", exc)
    if m and hasattr(m, "AgentKernel"):
        try:
            return m.AgentKernel()
        except Exception as exc:
            logger.debug("runtime: _get_kernel: Check if already in sys.modules (imported as si: %s", exc)
    return None


def _get_planner():
    m = _lazy("planner")
    if m and hasattr(m, "get_planner"):
        try:
            return m.get_planner()
        except Exception as exc:
            logger.debug("runtime: _get_planner: %s", exc)
    return None


def _get_ooda():
    m = _lazy("ooda_loop")
    if m and hasattr(m, "get_ooda"):
        try:
            return m.get_ooda()
        except Exception as exc:
            logger.debug("runtime: _get_ooda: %s", exc)
    return None


def _get_telemetry():
    m = _lazy("telemetry")
    if m and hasattr(m, "Telemetry"):
        try:
            return m.Telemetry()
        except Exception as exc:
            logger.debug("runtime: _get_telemetry: %s", exc)
    return None


def _get_policy():
    m = _lazy("policy_engine")
    if m and hasattr(m, "get_policy_engine"):
        try:
            return m.get_policy_engine()
        except Exception as exc:
            logger.debug("runtime: _get_policy: %s", exc)
    return None


def _get_world_model():
    m = _lazy("world_model")
    if m and hasattr(m, "get_world_model"):
        try:
            return m.get_world_model()
        except Exception as exc:
            logger.debug("runtime: _get_world_model: %s", exc)
    return None


def _get_tool_registry():
    m = _lazy("tool_registry")
    if m and hasattr(m, "ToolRegistry"):
        try:
            return m.ToolRegistry()
        except Exception as exc:
            logger.debug("runtime: tool_registry.ToolRegistry() failed: %s", exc)
    return None


def _get_reflection():
    m = _lazy("reflection_engine")
    if m and hasattr(m, "get_reflection"):
        try:
            return m.get_reflection()
        except Exception as exc:
            logger.debug("runtime: _get_reflection: %s", exc)
    return None


def _get_experience():
    m = _lazy("experience_manager")
    if m and hasattr(m, "get_experience"):
        try:
            return m.get_experience()
        except Exception as exc:
            logger.debug("runtime: _get_experience: %s", exc)
    return None


def _get_memory_manager():
    m = _lazy("memory_manager")
    if m and hasattr(m, "MemoryManager"):
        try:
            return m.MemoryManager()
        except Exception as exc:
            logger.debug("runtime: _get_memory_manager: %s", exc)
    return None


def _get_drift():
    m = _lazy("drift_analyzer")
    if m and hasattr(m, "get_drift_analyzer"):
        try:
            return m.get_drift_analyzer()
        except Exception as exc:
            logger.debug("runtime: _get_drift: %s", exc)
    return None


def _get_watchdog():
    m = _lazy("watchdog")
    if m and hasattr(m, "Watchdog"):
        try:
            return m.Watchdog()
        except Exception as exc:
            logger.debug("runtime: _get_watchdog: %s", exc)
    return None


def _get_goal_manager():
    m = _lazy("goal_manager")
    if m and hasattr(m, "GoalManager"):
        try:
            return m.GoalManager()
        except Exception as exc:
            logger.debug("runtime: _get_goal_manager: %s", exc)
    return None


def _get_supervisor():
    m = _lazy("runtime_supervisor")
    if m and hasattr(m, "get_supervisor"):
        try:
            return m.get_supervisor()
        except Exception as exc:
            logger.debug("runtime: _get_supervisor: %s", exc)
    return None


def _get_self_obs():
    m = _lazy("self_observation")
    if m:
        if hasattr(m, "get_self_observation"):
            try:
                return m.get_self_observation()
            except Exception as exc:
                logger.debug("runtime: _get_self_obs: %s", exc)
        if hasattr(m, "SelfObservationLoop"):
            try:
                return m.SelfObservationLoop()
            except Exception as exc:
                logger.debug("runtime: _get_self_obs: %s", exc)
    return None


def _get_task_graph():
    m = _lazy("task_graph")
    if m and hasattr(m, "get_engine"):
        try:
            return m.get_engine()
        except Exception as exc:
            logger.debug("runtime: _get_task_graph: %s", exc)
    return None


# ---------------------------------------------------------------------------
# ExecutionResult
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Structured result of a full tool execution through the hot path."""

    tool_name: str
    success: bool
    output: str = ""
    error: str = ""
    duration_s: float = 0.0
    task_id: str = ""
    policy_allowed: bool = True
    policy_reason: str = ""
    world_state: dict = field(default_factory=dict)
    violations: list = field(default_factory=list)
    reflection_id: str = ""
    stability_after: float = 1.0
    behavior_controls: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# RuntimeHotPath — THE single entry for every tool execution
# ---------------------------------------------------------------------------


class RuntimeHotPath:
    """Singleton that orchestrates the full tool execution pipeline.

    Every Hermes tool call should pass through:
        execute_tool(tool_name, args) → ExecutionResult

    Pipeline:
    1. PolicyEngine.check_action() — is this action allowed?
    2. WorldModel.snapshot() — what's the current environment state?
    3. RuntimeSupervisor.check_budget() — are we within resource limits?
    4. ToolRegistry.lookup() — is this tool registered? What's its risk?
    5. GoalManager — update goal tracking
    6. MemoryManager auto-retrieval — inject relevant memories
    7. EXECUTE — delegate to actual tool handler
    8. Telemetry.record() — log system metrics
    9. ReflectionEngine.reflect_on_task() — analyze what happened
    10. ExperienceManager — record success/failure
    11. MemoryManager — store results
    12. DriftAnalyzer.observe() — detect cognitive drift
    13. StabilityController — adjust runtime behavior based on stability
    14. EventBus — publish completion event
    """

    _instance: Optional["RuntimeHotPath"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    obj = super().__new__(cls)
                    obj._initialized = False
                    cls._instance = obj
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        self._kernel = None
        self._event_bus = None
        self._active_tasks: Dict[str, float] = {}
        self._task_counter = 0
        self._task_lock = threading.Lock()

        # Subsystem references — filled lazily on first use
        self._planner = None
        self._ooda = None
        self._telemetry = None
        self._policy = None
        self._world_model = None
        self._tool_registry = None
        self._reflection = None
        self._experience = None
        self._memory = None
        self._drift = None
        self._watchdog = None
        self._goals = None
        self._supervisor = None
        self._self_obs = None
        self._task_graph = None
        self._kernel = None

        # Lazy init tracking
        self._subsystems_initialized = False
        self._subsystem_init_lock = threading.Lock()

        self._initialized = True
        logger.info("runtime: RuntimeHotPath initialized (lazy)")

    def _ensure_subsystems(self):
        """Ensure subsystems are initialized (lazy, idempotent)."""
        if self._subsystems_initialized:
            return
        with self._subsystem_init_lock:
            if self._subsystems_initialized:
                return
            self._init_subsystems()
            self._subsystems_initialized = True

    def _init_subsystems(self):
        """Lazy-init all subsystem references (graceful on failure)."""
        # Kernel (if available, central orchestrator)
        try:
            self._kernel = _get_kernel()
            if self._kernel and hasattr(self._kernel, "initialize"):
                self._kernel.initialize()
        except Exception as exc:
            logger.debug("runtime: kernel init: %s", exc)
            self._kernel = None

        # EventBus — central nervous system
        try:
            eb_mod = _lazy("event_bus")
            if eb_mod and hasattr(eb_mod, "EventBus"):
                self._event_bus = eb_mod.EventBus()
                logger.info("runtime: EventBus connected")
        except Exception as exc:
            logger.debug("runtime: EventBus init: %s", exc)
            self._event_bus = None

        # Cached subsystem accessors for hot-path speed
        self._planner = _get_planner()
        # Load persisted planner preferences from disk
        if self._planner is not None:
            try:
                import json as _json
                _prefs_path = os.path.join(
                    os.path.expanduser("~/.hermes/core/data"),
                    "planner_preferences.json",
                )
                if os.path.exists(_prefs_path):
                    with open(_prefs_path) as f:
                        loaded = _json.load(f)
                    if hasattr(self._planner, "_tool_preferences"):
                        self._planner._tool_preferences.update(loaded)
                        logger.info("hermes-core: loaded %d planner preferences", len(loaded))
            except Exception as exc:
                logger.debug("hermes-core: prefs load failed: %s", exc)
        self._ooda = _get_ooda()
        self._telemetry = _get_telemetry()
        self._policy = _get_policy()
        self._world_model = _get_world_model()
        self._tool_registry = _get_tool_registry()
        self._reflection = _get_reflection()
        self._experience = _get_experience()
        self._memory = _get_memory_manager()
        self._drift = _get_drift()
        self._watchdog = _get_watchdog()
        self._goals = _get_goal_manager()
        self._supervisor = _get_supervisor()
        self._self_obs = _get_self_obs()
        self._task_graph = _get_task_graph()

        # Start RuntimeSupervisor if available
        try:
            if self._supervisor and hasattr(self._supervisor, "start"):
                self._supervisor.start()
        except Exception as exc:
            logger.debug("runtime: Start RuntimeSupervisor if available: %s", exc)

        # Start Watchdog if available
        try:
            if self._watchdog and hasattr(self._watchdog, "start"):
                self._watchdog.start()
        except Exception as exc:
            logger.debug("runtime: Start Watchdog if available: %s", exc)

        # Start SelfObservation if available
        try:
            if self._self_obs and hasattr(self._self_obs, "start"):
                self._self_obs.start()
        except Exception as exc:
            logger.debug("runtime: Start SelfObservation if available: %s", exc)

    # ------------------------------------------------------------------
    # PUBLIC API — the one method the agent should call
    # ------------------------------------------------------------------

    def execute_tool(
        self,
        tool_name: str,
        args: Optional[Dict[str, Any]] = None,
        handler: Optional[callable] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> ExecutionResult:
        """Execute a tool through the full Cognitive Runtime pipeline.

        Parameters
        ----------
        tool_name : str
            The tool/capability name (e.g. 'terminal', 'web_search').
        args : dict or None
            Tool arguments.
        handler : callable or None
            The actual tool handler function to call. If None, skips execution
            and returns only the pre-check result (audit mode).
        context : dict or None
            Additional context (goal, session_id, user_preferences).

        Returns
        -------
        ExecutionResult
        """
        args = args or {}
        ctx = context or {}

        # Ensure subsystems are initialized (lazy)
        self._ensure_subsystems()

        start_time = time.time()
        tool_id = self._new_task_id(tool_name)
        violations: List[str] = []
        behavior_controls: Dict[str, Any] = {}

        # --------------------------------------------------------------
        # STEP 0: Register with GoalManager
        # --------------------------------------------------------------
        try:
            if self._goals:
                goal_desc = ctx.get("goal", "") or args.get("goal", "") or tool_name
                goal_priority = ctx.get("priority", 50)
                self._goals.register_goal(
                    goal_id=tool_id,
                    description=f"tool:{tool_name} - {goal_desc[:80]}",
                    priority=goal_priority,
                )
        except Exception as exc:
            logger.debug("runtime: ------------------------------------------------------------: %s", exc)

        # --------------------------------------------------------------
        # STEP 1: PolicyEngine.check_action()
        # --------------------------------------------------------------
        policy_allowed = True
        policy_reason = ""
        try:
            if self._policy is not None:
                policy_result = self._policy.check_action(tool_name, args)
                policy_allowed = policy_result.get("allowed", True)
                policy_reason = policy_result.get("reason", "")
                if not policy_allowed:
                    violations.append(f"policy_denied: {policy_reason}")
                    # Publish policy blocked event
                    self._publish_event("policy.blocked", {
                        "tool": tool_name,
                        "reason": policy_reason,
                        "task_id": tool_id,
                    })
        except Exception as exc:
            policy_allowed = False
            policy_reason = f"policy_engine_error: {exc}"
            violations.append(f"policy_check_error: {exc}")
            logger.warning("runtime: PolicyEngine exception, fail-closed: %s", exc)

        # If blocked, return immediately with audit trail
        if not policy_allowed:
            duration = time.time() - start_time
            return ExecutionResult(
                tool_name=tool_name,
                success=False,
                output="",
                error=f"Policy denied: {policy_reason}",
                duration_s=duration,
                task_id=tool_id,
                policy_allowed=False,
                policy_reason=policy_reason,
                violations=violations,
            )

        # --------------------------------------------------------------
        # STEP 2: WorldModel Validation
        # --------------------------------------------------------------
        world_state: Dict[str, Any] = {}
        try:
            if self._world_model is not None:
                world_state = self._world_model.get_world_state(refresh=True)
        except Exception as exc:
            violations.append(f"world_model_error: {exc}")

        # --------------------------------------------------------------
        # STEP 3: RuntimeSupervisor Budget Check
        # --------------------------------------------------------------
        try:
            if self._supervisor is not None and hasattr(self._supervisor, "check_budget"):
                budget_ok, budget_msg = self._supervisor.check_budget()
                if not budget_ok:
                    violations.append(f"budget_exceeded: {budget_msg}")
                    self._publish_event("resource.warning", {
                        "tool": tool_name,
                        "message": budget_msg,
                        "task_id": tool_id,
                    })
        except Exception as exc:
            violations.append(f"budget_check_error: {exc}")

        # --------------------------------------------------------------
        # STEP 4: ToolRegistry Capability/Risk Check
        # --------------------------------------------------------------
        try:
            if self._tool_registry is not None:
                capabilities = self._tool_registry.find(query=tool_name)
                if not capabilities:
                    violations.append(f"tool_not_registered: {tool_name}")
        except Exception as exc:
            violations.append(f"tool_registry_error: {exc}")

        # --------------------------------------------------------------
        # STEP 5: Memory Auto-Injection (retrieve relevant context)
        # --------------------------------------------------------------
        memory_context = self._retrieve_memory_context(tool_name, args, ctx)

        # --------------------------------------------------------------
        # STEP 6: Stability → Behavior Control (homeostasis)
        # --------------------------------------------------------------
        try:
            stability = self._get_stability_score()
            behavior_controls = self._compute_behavior_controls(stability)
            if behavior_controls.get("throttle"):
                violations.append(f"stability_throttle: stability={stability:.2f} < {behavior_controls.get('throttle_at', 0.7)}")
        except Exception as exc:
            violations.append(f"stability_check_error: {exc}")

        # --------------------------------------------------------------
        # STEP 7: EXECUTE the actual tool
        # --------------------------------------------------------------
        output = ""
        error = ""
        success = True

        if handler is not None:
            try:
                enriched_args = dict(args)
                if behavior_controls:
                    enriched_args["_behavior_controls"] = behavior_controls

                result = handler(**enriched_args)
                if isinstance(result, str):
                    output = result
                elif isinstance(result, dict):
                    output = json.dumps(result, indent=2, ensure_ascii=False, default=str)
                else:
                    output = str(result)
                success = True
            except Exception as exc:
                error = str(exc)
                success = False
                violations.append(f"execution_error: {error}")
        else:
            # Audit mode — no handler provided
            output = "[audit mode — no execution]"
            success = True

        duration = time.time() - start_time

        # --------------------------------------------------------------
        # STEP 8: Telemetry Record
        # --------------------------------------------------------------
        try:
            if self._telemetry is not None:
                if hasattr(self._telemetry, "record_tool"):
                    self._telemetry.record_tool(
                        tool_name=tool_name,
                        success=success,
                        duration_s=duration,
                    )
                elif hasattr(self._telemetry, "collect"):
                    self._telemetry.collect()
        except Exception as exc:
            logger.debug("runtime: ------------------------------------------------------------: %s", exc)

        # --------------------------------------------------------------
        # STEP 9: Kernel after_task (if kernel available)
        # --------------------------------------------------------------
        try:
            if self._kernel is not None and hasattr(self._kernel, "after_task"):
                self._kernel.after_task(
                    task_id=tool_id,
                    result={
                        "success": success,
                        "summary": output[:500] if output else (error or "no output"),
                        "error": error or None,
                        "duration_s": duration,
                        "tools_used": [{"name": tool_name, "success": success}],
                    },
                    context={
                        "goal": ctx.get("goal", ""),
                        "task_type": tool_name,
                        "domain": ctx.get("domain", "general"),
                        "has_memory_context": bool(memory_context),
                    },
                )
        except Exception as exc:
            logger.debug("runtime: ------------------------------------------------------------: %s", exc)

        # --------------------------------------------------------------
        # STEP 10: DriftAnalyzer Observe (active defense)
        # --------------------------------------------------------------
        drift_report = {}
        try:
            if self._drift is not None:
                # DriftAnalyzer has get_summary()/analyze_all(), not observe()
                if hasattr(self._drift, "get_summary"):
                    drift_report = self._drift.get_summary()
                elif hasattr(self._drift, "analyze_all"):
                    drift_report = self._drift.analyze_all()
                # If drift detected, trigger active defense
                if drift_report and drift_report.get("action_required"):
                    self._apply_active_defense(drift_report)
                    violations.append(f"drift_action: {drift_report.get('action', 'none')}")
        except Exception as exc:
            logger.debug("runtime: If drift detected, trigger active defense: %s", exc)

        # --------------------------------------------------------------
        # STEP 11: Learning Loop — Reflection → Experience → Preference
        # --------------------------------------------------------------
        self._run_learning_loop(tool_name, tool_id, success, output, error, duration, ctx, memory_context=memory_context)

        # --------------------------------------------------------------
        # STEP 12: Update GoalManager status
        # --------------------------------------------------------------
        try:
            if self._goals:
                self._goals.update_goal(
                    goal_id=tool_id,
                    status="completed" if success else "failed",
                    result_summary=output[:500] if success else error,
                )
        except Exception as exc:
            logger.debug("runtime: GoalManager update: %s", exc)

        # --------------------------------------------------------------
        # STEP 12b: TaskGraph — record tool call as task node
        # --------------------------------------------------------------
        try:
            if self._task_graph is not None and hasattr(self._task_graph, "add_node"):
                _tg_mod = sys.modules.get("task_graph")
                if _tg_mod is not None and hasattr(_tg_mod, "TaskNode"):
                    from datetime import datetime, timezone as _tz
                    _now = datetime.now(_tz.utc).isoformat()
                    _node_id = f"hp_{tool_id}"
                    _graph_id = f"session_{ctx.get('session_id', 'default')}"
                    if not hasattr(self, "_session_graph_id"):
                        self._session_graph_id = None
                    if self._session_graph_id is None:
                        try:
                            self._session_graph_id = self._task_graph.create_graph(
                                name=_graph_id, nodes=[],
                            )
                        except Exception:
                            self._session_graph_id = _graph_id
                    _node = _tg_mod.TaskNode(
                        node_id=_node_id,
                        action=tool_name,
                        params={"args_keys": list(args.keys())},
                        depends_on=[],
                        status="completed" if success else "failed",
                        result={"output_len": len(output)} if success else None,
                        error=error[:200] if error else None,
                        started_at=_now,
                        completed_at=_now,
                    )
                    self._task_graph.add_node(self._session_graph_id, _node)
        except Exception as exc:
            logger.debug("runtime: TaskGraph add_node: %s", exc)

        # --------------------------------------------------------------
        # STEP 12c: Watchdog deadlock check (~10% sampling)
        # --------------------------------------------------------------
        try:
            if random.random() < 0.1:
                if self._watchdog is not None and hasattr(self._watchdog, "check_deadlocks"):
                    deadlocks = self._watchdog.check_deadlocks()
                    if deadlocks:
                        logger.warning("runtime: watchdog detected %d deadlocks", len(deadlocks))
        except Exception as exc:
            logger.debug("runtime: Watchdog deadlock check: %s", exc)

        # --------------------------------------------------------------
        # STEP 12d: SelfObservation (~5% sampling)
        # --------------------------------------------------------------
        try:
            if random.random() < 0.05:
                if self._self_obs is not None and hasattr(self._self_obs, "run_once"):
                    report = self._self_obs.run_once()
                    if report and hasattr(report, "alerts") and report.alerts:
                        logger.warning(
                            "runtime: self-obs alerts: %s",
                            "; ".join(report.alerts[:3]),
                        )
        except Exception as exc:
            logger.debug("runtime: SelfObservation run_once: %s", exc)

        # --------------------------------------------------------------
        # STEP 13: Publish completion event
        # --------------------------------------------------------------
        self._publish_event(
            "tool.completed" if success else "tool.failed",
            {
                "tool": tool_name,
                "task_id": tool_id,
                "duration_s": round(duration, 3),
                "success": success,
                "violations": violations,
            },
        )

        # --------------------------------------------------------------
        # Get final stability after this execution
        # --------------------------------------------------------------
        stability_after = self._get_stability_score()

        # Cleanup active task tracking
        with self._task_lock:
            self._active_tasks.pop(tool_id, None)

        return ExecutionResult(
            tool_name=tool_name,
            success=success,
            output=output,
            error=error,
            duration_s=duration,
            task_id=tool_id,
            policy_allowed=True,
            violations=violations,
            stability_after=stability_after,
            behavior_controls=behavior_controls,
        )

    # ------------------------------------------------------------------
    # Memory Auto-Injection
    # ------------------------------------------------------------------

    def _retrieve_memory_context(
        self,
        tool_name: str,
        args: Dict[str, Any],
        ctx: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Retrieve relevant memories before tool execution and return as context."""
        memory_context: Dict[str, Any] = {
            "retrieved_memories": [],
            "similar_failures": [],
            "successful_patterns": [],
        }

        if self._memory is None:
            return memory_context

        try:
            goal = ctx.get("goal", "") or args.get("goal", "") or tool_name

            # 1. Retrieve relevant episodic memories (semantic search)
            if hasattr(self._memory, "recall_episodes"):
                results = self._memory.recall_episodes(query=goal, limit=5)
                if results:
                    memory_context["retrieved_memories"] = results

            # 2. Retrieve similar failures from episodic memory
            if hasattr(self._memory, "recall_episodes"):
                failures = self._memory.recall_episodes(
                    query=f"failed {tool_name}", limit=3
                )
                if failures:
                    memory_context["similar_failures"] = failures

            # 3. Retrieve successful patterns from experience manager
            if self._experience is not None and hasattr(self._experience, "get_strategies"):
                strategies = self._experience.get_strategies(
                    domain=ctx.get("domain", "general"),
                    min_success_rate=0.3,
                )
                if strategies:
                    memory_context["successful_patterns"] = strategies[:3]

        except Exception as exc:
            logger.debug("runtime: memory retrieval error: %s", exc)

        return memory_context

    # ------------------------------------------------------------------
    # Stability → Behavior Control (Homeostasis)
    # ------------------------------------------------------------------

    def _get_stability_score(self) -> float:
        """Get the current cognitive stability score (0.0–1.0).
        Defaults to 1.0 when no telemetry data is available (startup).
        """
        try:
            if self._telemetry is not None:
                if hasattr(self._telemetry, "get_health_score"):
                    score = self._telemetry.get_health_score()
                    if score is not None and score > 0:
                        return min(1.0, float(score))
                if hasattr(self._telemetry, "get_latest"):
                    latest = self._telemetry.get_latest()
                    if latest is not None and hasattr(latest, "cognitive_stability_score"):
                        s = latest.cognitive_stability_score
                        if s is not None and s > 0:
                            return min(1.0, float(s))
        except Exception as exc:
            logger.debug("runtime: _get_stability_score: --------------------------------------: %s", exc)
        return 1.0  # default: assume stable when no data

    def _compute_behavior_controls(self, stability: float) -> Dict[str, Any]:
        """Map stability score to runtime behavior controls.

        | Score   | Behavior                                   |
        |---------|---------------------------------------------|
        | ≥ 0.7   | Normal operation (no restrictions)          |
        | 0.5–0.7 | Reduced planner depth, reduced parallelism  |
        | 0.3–0.5 | Safe mode — disable autonomous loop         |
        | < 0.3   | Freeze non-critical, trigger recovery       |
        """
        controls: Dict[str, Any] = {
            "stability": round(stability, 3),
            "throttle": False,
            "safe_mode": False,
            "freeze": False,
            "recovery": False,
            "planner_depth_reduction": 0,
            "parallelism_reduction": 0,
        }

        if stability >= 0.7:
            controls["mode"] = "normal"
        elif stability >= 0.5:
            controls["mode"] = "reduced"
            controls["throttle"] = True
            controls["throttle_at"] = 0.7
            controls["planner_depth_reduction"] = 2
            controls["parallelism_reduction"] = 1
        elif stability >= 0.3:
            controls["mode"] = "safe"
            controls["throttle"] = True
            controls["safe_mode"] = True
            controls["throttle_at"] = 0.5
            controls["planner_depth_reduction"] = 3
            controls["parallelism_reduction"] = 2
        else:
            controls["mode"] = "frozen"
            controls["throttle"] = True
            controls["safe_mode"] = True
            controls["freeze"] = True
            controls["recovery"] = True
            controls["throttle_at"] = 0.3

        return controls

    # ------------------------------------------------------------------
    # Active Defense — DriftAnalyzer actions
    # ------------------------------------------------------------------

    def _apply_active_defense(self, drift_report: Dict[str, Any]) -> None:
        """Apply active defense actions based on drift analysis."""
        action = drift_report.get("action", "none")
        target = drift_report.get("target", "")

        if action == "prune_memory" and self._memory is not None:
            try:
                if hasattr(self._memory, "prune"):
                    self._memory.prune()
                logger.info("runtime: active defense — pruned memory")
            except Exception as exc:
                logger.debug("runtime: active defense prune_memory: %s", exc)

        elif action == "reset_planner_weights" and self._planner is not None:
            try:
                if hasattr(self._planner, "_strategy_preferences"):
                    self._planner._strategy_preferences = []
                if hasattr(self._planner, "_tool_preferences"):
                    self._planner._tool_preferences = {}
                # Remove persisted preferences file
                _prefs_path = os.path.join(
                    os.path.expanduser("~/.hermes/core/data"),
                    "planner_preferences.json",
                )
                if os.path.exists(_prefs_path):
                    os.remove(_prefs_path)
                logger.info("runtime: active defense — reset planner weights + persisted prefs")
            except Exception as exc:
                logger.debug("runtime: active defense reset_planner_weights: %s", exc)

        elif action == "throttle_eventbus":
            try:
                if self._event_bus is not None:
                    if hasattr(self._event_bus, "set_rate_limit"):
                        self._event_bus.set_rate_limit(max_events_per_second=5)
                        logger.info("runtime: active defense — throttled EventBus via rate limit")
                    else:
                        logger.info("runtime: active defense — EventBus throttle skipped (no set_rate_limit)")
            except Exception as exc:
                logger.debug("runtime: active defense throttle_eventbus: %s", exc)

        elif action == "reduce_reflection_frequency" and self._reflection is not None:
            # ReflectionEngine has no set_frequency(); log diagnostic instead
            try:
                stats = self._reflection.get_stats() if hasattr(self._reflection, "get_stats") else {}
                logger.info(
                    "runtime: active defense — reflection diagnostic (freq reduction unavailable): %s",
                    stats,
                )
            except Exception as exc:
                logger.debug("runtime: active defense reduce_reflection: %s", exc)

        elif action == "recovery" and self._kernel is not None:
            try:
                if hasattr(self._kernel, "recover"):
                    self._kernel.recover()
                logger.info("runtime: active defense — triggered recovery")
            except Exception as exc:
                logger.debug("runtime: active defense recovery: %s", exc)

    # ------------------------------------------------------------------
    # Learning Loop — Reflection → Experience → Planner Preference
    # ------------------------------------------------------------------

    def _run_learning_loop(
        self,
        tool_name: str,
        task_id: str,
        success: bool,
        output: str,
        error: str,
        duration: float,
        ctx: Dict[str, Any],
        memory_context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Run the post-execution learning loop.

        This is the core of the self-improvement cycle:
            1. ReflectionEngine.reflect_on_task() — analyze what happened
            2. ExperienceManager — record success/failure with confidence
            3. Planner preference update — adjust future strategy weights
        """
        goal = ctx.get("goal", "") or tool_name
        mem = memory_context or {}

        # --- 1. Reflection (now with memory context) ---
        reflection_id = ""
        try:
            if self._reflection is not None and hasattr(self._reflection, "reflect_on_task"):
                reflection_ctx = dict(ctx)
                if mem.get("retrieved_memories"):
                    reflection_ctx["related_memories"] = mem["retrieved_memories"][:3]
                if mem.get("similar_failures"):
                    reflection_ctx["past_failures"] = mem["similar_failures"][:3]
                reflection_id = self._reflection.reflect_on_task(
                    task_id=task_id,
                    goal=goal,
                    result={
                        "success": success,
                        "summary": (output[:1000] if output else (error or "no output")),
                        "error": error or None,
                        "duration_s": duration,
                        "tools_used": [tool_name],
                    },
                    context=reflection_ctx,
                )
        except Exception as exc:
            logger.debug("runtime: --- 1. Reflection (now with memory context) ---: %s", exc)

        # --- 2. Experience update with confidence (now with failure patterns) ---
        try:
            if self._experience is not None:
                domain = ctx.get("domain", "general")
                tags = ctx.get("tags", [tool_name])

                if success:
                    if hasattr(self._experience, "record_success"):
                        self._experience.record_success(
                            pattern_name=f"tool:{tool_name}",
                            action_sequence=[tool_name],
                            duration_s=duration,
                            domain=domain,
                            tags=tags,
                        )
                else:
                    if hasattr(self._experience, "record_failure"):
                        # Enrich failure with similar past failures
                        error_msg = error or "Unknown error"
                        similar = mem.get("similar_failures", [])
                        if similar:
                            error_msg += f" [similar past failures: {len(similar)}]"
                        self._experience.record_failure(
                            domain=domain,
                            error_type=f"tool_failure:{tool_name}",
                            error_message=error_msg,
                        )
        except Exception as exc:
            logger.debug("runtime: Enrich failure with similar past failures: %s", exc)

        # --- 3. Planner preference update (via ExperienceManager) ---
        try:
            if self._experience is not None and hasattr(self._experience, "record_tool_usage"):
                # Record tool usage in ExperienceManager — this feeds into
                # Planner._select_tool() via exp.get_tool_stats()
                self._experience.record_tool_usage(
                    tool_name=tool_name,
                    domain=ctx.get("domain", "general"),
                    success=success,
                    duration_s=duration,
                )
            # Also update in-memory planner preferences for immediate effect
            if self._planner is not None and hasattr(self._planner, "_tool_preferences"):
                prefs = self._planner._tool_preferences
                current = prefs.get(tool_name, 0.5)
                if success:
                    prefs[tool_name] = min(1.0, current + 0.05)
                else:
                    prefs[tool_name] = max(0.0, current - 0.05)
                # Persist to disk so learning survives restarts
                try:
                    _prefs_path = os.path.join(
                        os.path.expanduser("~/.hermes/core/data"),
                        "planner_preferences.json",
                    )
                    os.makedirs(os.path.dirname(_prefs_path), exist_ok=True)
                    with open(_prefs_path, "w") as _f:
                        json.dump(
                            {k: round(v, 4) for k, v in prefs.items()},
                            _f, indent=2,
                        )
                except Exception as exc:
                    logger.debug("runtime: prefs persist: %s", exc)
        except Exception as exc:
            logger.debug("runtime: Planner preference update: %s", exc)

        # --- 4. Memory consolidation ---
        try:
            if self._memory is not None and hasattr(self._memory, "remember_episode"):
                self._memory.remember_episode(
                    description=f"Tool execution: {tool_name}",
                    summary=output[:500] if success else error,
                    outcome="success" if success else "failure",
                    tags=[tool_name, "tool_execution"],
                )
        except Exception as exc:
            logger.debug("runtime: --- 4. Memory consolidation ---: %s", exc)

    # ------------------------------------------------------------------
    # EventBus publishing
    # ------------------------------------------------------------------

    def _publish_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publish an event to the EventBus (if available)."""
        try:
            if self._event_bus is not None and hasattr(self._event_bus, "publish"):
                self._event_bus.publish(event_type, data)
        except Exception as exc:
            logger.debug("runtime: _publish_event: --------------------------------------------: %s", exc)

    # ------------------------------------------------------------------
    # Task ID generation
    # ------------------------------------------------------------------

    def _new_task_id(self, tool_name: str) -> str:
        with self._task_lock:
            self._task_counter += 1
            tid = f"hp_{int(time.time())}_{self._task_counter}_{tool_name[:16]}"
            self._active_tasks[tid] = time.time()
            return tid

    # ------------------------------------------------------------------
    # Status / Health
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return runtime status summary."""
        self._ensure_subsystems()

        stability = self._get_stability_score()
        controls = self._compute_behavior_controls(stability)

        output = {
            "hot_path_active": True,
            "stability_score": round(stability, 3),
            "behavior_mode": controls.get("mode", "unknown"),
            "active_tasks": len(self._active_tasks),
            "subsystems": {
                "kernel": self._kernel is not None,
                "planner": self._planner is not None,
                "ooda": self._ooda is not None,
                "telemetry": self._telemetry is not None,
                "policy": self._policy is not None,
                "world_model": self._world_model is not None,
                "tool_registry": self._tool_registry is not None,
                "reflection": self._reflection is not None,
                "experience": self._experience is not None,
                "memory": self._memory is not None,
                "drift": self._drift is not None,
                "watchdog": self._watchdog is not None,
                "goals": self._goals is not None,
                "supervisor": self._supervisor is not None,
                "self_observation": self._self_obs is not None,
                "task_graph": self._task_graph is not None,
            },
            "behavior_controls": controls,
        }

        # Include planner preferences if available
        try:
            if self._planner and hasattr(self._planner, "_tool_preferences"):
                output["planner_tool_preferences"] = {
                    k: round(v, 3) for k, v in self._planner._tool_preferences.items()
                }
        except Exception as exc:
            logger.debug("runtime: Include planner preferences if available: %s", exc)

        return output

    def get_runtime_telemetry(self) -> Dict[str, Any]:
        """Get runtime performance telemetry."""
        return {
            "active_tasks": dict(self._active_tasks),
            "total_tasks_executed": self._task_counter,
            "current_stability": self._get_stability_score(),
            "behavior_mode": self._compute_behavior_controls(self._get_stability_score()).get("mode", "unknown"),
            "subsystems_available": sum(
                1 for v in self.get_status().get("subsystems", {}).values() if v
            ),
            "subsystems_total": len(self.get_status().get("subsystems", {})),
        }


# ---------------------------------------------------------------------------
# Module-level shortcut
# ---------------------------------------------------------------------------

def get_runtime() -> RuntimeHotPath:
    """Return the RuntimeHotPath singleton."""
    return RuntimeHotPath()


def execute_tool(
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    handler: Optional[callable] = None,
    context: Optional[Dict[str, Any]] = None,
) -> ExecutionResult:
    """Convenience — execute a tool through the full runtime pipeline."""
    return get_runtime().execute_tool(
        tool_name=tool_name,
        args=args,
        handler=handler,
        context=context,
    )