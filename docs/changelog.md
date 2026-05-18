# Changelog

## 2026-05-18: hermes-core 插件审计与修复

### 背景

对 `~/.hermes/plugins/hermes-core/` 的 hooks.py (510行) 和 runtime_integration.py (1100行) 进行了全面审计，发现 7 个影响学习循环有效性的 bug。

### hooks.py 修复 (4 项)

#### Bug #1: success 硬编码为 True (严重)

**问题**: `post_tool_call` hook 中 `success = True` 硬编码，学习循环永远认为工具调用成功。导致：
- ExperienceManager 从不记录失败模式
- Planner 权重只升不降
- DriftAnalyzer 无法检测失败趋势

**修复**: 分析输出内容中的 11 种失败关键词 (traceback, error:, exception:, failed, permission denied, not found, no such file, connection refused, timed out, exit code 1, exit_code=1)。命中任一关键词则标记为失败。

#### Bug #2: duration 硬编码为 0.0 (中等)

**问题**: 所有工具耗时记录为 0，Telemetry 和 DriftAnalyzer 的性能数据全部无效。

**修复**: 在 `pre_tool_call` 中记录 `time.time()` 到 `_call_timers` 字典，在 `post_tool_call` 中计算真实耗时并传递给学习循环。

#### Bug #3: 漂移采样使用 id() (中等)

**问题**: `id(tool_name) % 10` 使用内存地址做采样，CPython 中短字符串的 id 可能恒定，导致采样率不确定——可能每次触发或永远不触发。

**修复**: 改为 `hash(tool_call_id or tool_name) % 10`，确定性采样率稳定在 ~10%。

#### Bug #4: JSON 序列化在截断之前 (低)

**问题**: 先将整个 result 做 `json.dumps()` (可能产生巨大字符串)，再截断到 200 字符。浪费 CPU。

**修复**: 先 `str(result)[:500]` 截断，再传递给学习循环。输出上限从 200 扩大到 300 字符以给反思引擎更多上下文。

### runtime_integration.py 修复 (3 项)

#### Bug #5: memory_context 注入 handler kwargs (中等)

**问题**: `_retrieve_memory_context()` 的结果被注入到 `enriched_args["_memory_context"]`，然后传给标准工具 handler。标准工具 (terminal, read_file 等) 不认识这个参数，可能抛 TypeError (被 except 吞掉)。

**修复**: 移除 `_memory_context` 注入到 handler args 的逻辑。memory_context 仍然被检索，但不再传给 handler。

#### Bug #6: `**memory_context` 散入 kernel.after_task (中等)

**问题**: `kernel.after_task(context={..., **memory_context})` 将 `retrieved_memories`, `similar_failures`, `successful_patterns` 三个 key 散入 context dict，可能导致 after_task 内部逻辑异常。

**修复**: 改为 `"has_memory_context": bool(memory_context)`，只传递是否有记忆上下文的布尔值。

#### Bug #7: Planner 偏好内存驻留、重启丢失 (严重)

**问题**: `_tool_preferences` 是普通 Python dict，每次进程重启后所有学习到的工具偏好归零。学习循环形同虚设。

**修复**: 
- 每次偏好更新后写入 `~/.hermes/core/data/planner_preferences.json`
- RuntimeHotPath 初始化时从磁盘加载偏好

### 验证结果

```
Test 1: Success Detection   ✅ 7/7 case passed
Test 2: Duration Tracking   ✅ 50ms sleep → 50.2ms measured
Test 3: Hash Sampling       ✅ 6.0% rate (within 5-15% expected)
Test 4: Preferences File    ✅ JSON persistence ready
```

### 资源影响

| 项目 | 开销 |
|------|------|
| 额外 LLM 调用 | 0 |
| CPU 增量 | < 50ms / 工具调用 |
| 内存增量 | < 1MB |
| Token 增量 | 0 |

### 仍存在的已知限制

1. EventBus handler (tool.completed, tool.failed) 仍是 no-op
2. 4 个子系统 (ooda, watchdog, task_graph, self_obs) 初始化但未被 execute_tool 使用
3. 17+ bare except: pass 仍在 runtime_integration.py 中 (未全部清理)
4. PolicyEngine check 失败时 fail-open (允许执行)
5. 反思和经验数据的"质量"取决于输出截断长度 (300 字符)
