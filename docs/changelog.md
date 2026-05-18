# Changelog

## 2026-05-18: hermes-core 子系统接入 — 7 项已知限制修复

### 背景

第一轮审计修复了 7 个数据流 bug（见下方 2026-05-18 条目）。本轮修复 7 个已知限制，
将之前"初始化但未接入"的子系统连接到实际执行流，让学习循环从"只写不读"变为"读写闭环"。

### 修复清单

#### Fix #1: PolicyEngine fail-open → fail-closed (安全)

**问题**: PolicyEngine 抛异常时 `policy_allowed` 保持 `True`（初始值），异常 = 放行。
**修复**: except 块显式设 `policy_allowed = False`，附带 `policy_engine_error` 原因。

#### Fix #2: 记忆检索结果影响学习循环 (数据流)

**问题**: `_retrieve_memory_context()` 在 `execute_tool()` 中被调用，但 hooks.py 的
`post_tool_call` 从未调用 `execute_tool()`，记忆检索是死代码。
**修复**:
- hooks.py 的 `post_tool_call` 新增记忆检索步骤，调用 `_runtime._retrieve_memory_context()`
- 记忆上下文传入 `_run_learning_loop(memory_context=...)`
- 学习循环将 `related_memories` 和 `past_failures` 注入反思引擎的 context
- 失败记录附带类似失败计数

#### Fix #3: Watchdog/TaskGraph/SelfObservation 接入执行流 (架构)

**问题**: 三个子系统初始化后从未被调用。
**修复**:
- **Watchdog.check_deadlocks()** — `pre_tool_call` 中 10% 采样检查死锁
- **TaskGraph** — `post_tool_call` 中懒创建会话级任务图，每个工具调用记录为节点
- **SelfObservation.run_once()** — `post_tool_call` 中 5% 采样触发深度自观察
- **OODA** — 不接入每工具调用（会与 Planner+Reflection+Experience 重复工作），保留给复杂多步任务

#### Fix #4: EventBus handler 从 no-op 变为实际处理 (事件驱动)

**问题**: `_on_tool_completed_event` 是空函数，`_on_tool_failed_event` 只在 safe/frozen 模式记录。
**修复**:
- `tool.completed` → 记录到 event_logger 审计轨迹
- `tool.failed` → 记录 tool/mode/stability/violations，在 safe/frozen 模式触发 SelfObservation
- `recovery.triggered` → 触发 SelfObservation 做事后诊断，记录到 event_logger

#### Fix #5: 清理 bare except: pass (可维护性)

**问题**: runtime_integration.py 有 38 处 `except Exception: pass`，错误被静默吞掉。
**修复**: 全部替换为 `except Exception as exc: logger.debug("runtime: <context>: %s", exc)`，
每个日志消息包含函数名和最近的注释作为上下文。

#### Fix #6: 反思/经验截断长度提升 (数据质量)

**问题**: 输出截断 300 字符给反思引擎，200 字符给 kernel/goals/memory，复杂任务的反思质量差。
**修复**:
- 反思引擎: 300 → 1000 字符
- Kernel after_task / GoalManager / Memory: 200 → 500 字符

#### Fix #7: Planner 从 ExperienceManager 学习 (学习闭环)

**问题**: 学习循环的 `_tool_preferences ±0.05` 与 ExperienceManager 的 success_rate 是两套独立数据。
**修复**:
- 学习循环新增 `experience.record_tool_usage()` 调用，更新 ExperienceManager 的工具统计
- Planner 的 `_select_tool()` 已有的 `exp.get_tool_stats()` 查询自动获取最新数据
- 移除冗余的 JSON 磁盘持久化（ExperienceManager 自己管理持久化）

### 额外修复

- **DriftAnalyzer API 修复**: hooks.py 和 runtime_integration.py 中的 `drift.observe()` 调用
  改为 `drift.get_summary()` / `drift.analyze_all()`（DriftAnalyzer 没有 observe 方法）

### 资源影响

| 项目 | 开销 |
|------|------|
| 额外 LLM 调用 | 0 |
| CPU 增量 | < 100ms / 工具调用（含记忆检索） |
| 内存增量 | < 2MB |
| Token 增量 | 0 |

### 现在的数据流

```
用户说话 → LLM 决策 → 执行工具
  → pre_tool_call:
    → PolicyEngine (fail-closed)
    → WorldModel snapshot
    → Telemetry
    → Watchdog deadlock check (10%)
  → post_tool_call:
    → 成功/失败检测
    → Duration 计时
    → 记忆检索
    → 学习循环:
      → 反思引擎 (含记忆上下文)
      → 经验管理器 (success/failure + tool_usage)
      → Planner 偏好更新
    → DriftAnalyzer (10%)
    → TaskGraph 节点记录
    → SelfObservation (5%)
    → EventBus 事件发布
      → tool.completed/tool.failed handler
```

---

## 2026-05-18: hermes-core 插件审计与修复

### 背景

对 `~/.hermes/plugins/hermes-core/` 的 hooks.py (510行) 和 runtime_integration.py (1100行)
进行了全面审计，发现 7 个影响学习循环有效性的 bug。

### hooks.py 修复 (4 项)

#### Bug #1: success 硬编码为 True (严重)

**问题**: `post_tool_call` hook 中 `success = True` 硬编码，学习循环永远认为工具调用成功。
**修复**: 分析输出内容中的 11 种失败关键词。

#### Bug #2: duration 硬编码为 0.0 (中等)

**问题**: 所有工具耗时记录为 0。
**修复**: `pre_tool_call` 记录时间戳，`post_tool_call` 计算真实耗时。

#### Bug #3: 漂移采样使用 id() (中等)

**问题**: `id(tool_name) % 10` 使用内存地址做采样，采样率不确定。
**修复**: 改为 `hash(tool_call_id or tool_name) % 10`。

#### Bug #4: JSON 序列化在截断之前 (低)

**问题**: 先 `json.dumps()` 再截断，浪费 CPU。
**修复**: 先 `str(result)[:500]` 截断。

### runtime_integration.py 修复 (3 项)

#### Bug #5: memory_context 注入 handler kwargs (中等)

**修复**: 移除 `_memory_context` 注入到 handler args 的逻辑。

#### Bug #6: `**memory_context` 散入 kernel.after_task (中等)

**修复**: 改为 `"has_memory_context": bool(memory_context)`。

#### Bug #7: Planner 偏好重启丢失 (严重)

**修复**: 每次偏好更新后写入 JSON，初始化时从磁盘加载。
