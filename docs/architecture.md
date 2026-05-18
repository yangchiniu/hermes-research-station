# 技术架构：对原生 Hermes 的增强

本文档记录我们在 Hermes Agent 基础上构建的增强层——为什么做、做了什么、验证了什么。

## 核心理念

> 不 fork Hermes，不改源码。在 pip install hermes 之上，用插件和覆盖层解决原生 Agent 的六个短板。

原生 Hermes 是一个优秀的单 Agent 框架，但在长时间运行、多 Agent 协作、可观测性方面存在空白。我们的工作是补齐这些空白。

---

## 六维增强总览

| 维度 | 原生 Hermes 状态 | 我们的增强 | 验证状态 |
|------|-----------------|-----------|----------|
| 上下文管理 | 看得见但看不全 | AGENTS.md 聚合 + references/ 按需加载 | ✅ 154 skills, token 节省 50-77% |
| 工具系统 | 够用但零碎 | 16 子系统认知插件 + skill_router | ✅ 21,818 行, 16/16 子系统在线 |
| 执行编排 | 最弱环节 | hermes-runtime Supervisor/Worker | ✅ 25 tasks soak test, 0% 失败率 |
| 状态与记忆 | 做得好但容量有限 | 5 层记忆 + 4 SQLite 持久化 | ✅ 跨会话持久性已验证 |
| 评估与观测 | 几乎是盲的 | 遥测 + 漂移分析 + 反思引擎 | ✅ 12h 稳定性测试全绿 |
| 约束与恢复 | 有但很粗糙 | Watchdog + Goal 仲裁 + 自动恢复 | ✅ 8/8 混沌场景恢复成功 |

---

## 一、hermes-core 认知插件

### 架构

```
Tool Call → RuntimeHotPath.execute_tool()
  ├── GoalManager.register_goal()          ← 目标追踪
  ├── PolicyEngine.check_action()          ← 安全拦截
  ├── WorldModel.snapshot()                ← 环境感知
  ├── RuntimeSupervisor.check_budget()     ← 资源限额
  ├── ToolRegistry.lookup()                ← 能力校验
  ├── MemoryAutoInject                     ← 上下文注入
  ├── BehaviorControl                      ← 稳态闭环
  ├── EXECUTE (handler)                    ← 实际执行
  ├── Telemetry.record()                   ← 遥测记录
  ├── Kernel.after_task()                  ← 后处理
  ├── DriftAnalyzer.observe()              ← 漂移检测
  ├── LearningLoop                         ← 学习闭环
  │     ├── ReflectionEngine.reflect()
  │     ├── ExperienceManager.record()
  │     ├── Planner.preference_update()
  │     └── MemoryManager.store()
  └── EventBus.publish()                   ← 事件广播
```

### 模块清单 (27 模块, 21,818 行)

| 模块 | 行数 | 职责 |
|------|------|------|
| kernel.py | 1,300 | 编排器：12 子系统的 pre/post-task 管道 |
| planner.py | 1,822 | 目标分解 → 工具选择 → 约束推理 → 风险评估 |
| ooda_loop.py | 983 | Observe → Orient → Decide → Act 自主循环 |
| event_bus.py | 935 | 20+ 事件类型、线程安全、死信队列、限速、去重 |
| policy_engine.py | 897 | 5 层安全检查：禁止 → 风险 → 工具 → 域 → 资源 |
| world_model.py | 968 | CPU/RAM/Disk/网络/浏览器实时快照 + DB 持久化 |
| tool_registry.py | 589 | 能力注册表：风险/成本/认证分级 |
| reflection_engine.py | 649 | 错误分析 + 改进建议 + 模式提取 |
| experience_manager.py | 1,219 | 成功/失败模式追踪、置信度衰减、最优工具推荐 |
| memory_manager.py | 1,544 | 5 层记忆：working/episodic/semantic/procedural/environment |
| drift_analyzer.py | 992 | 5 维度加权漂移检测 + 自动防御 |
| watchdog.py | 762 | 死锁/活锁/Event Storm/递归规划检测 |
| goal_manager.py | 723 | 6 级优先级仲裁 (RECOVERY=100 → IDLE=0) |
| runtime_supervisor.py | 740 | 30 秒资源监控循环、阈值告警 |
| self_observation.py | 543 | 5 分钟周期自观察 + 自动告警 |
| task_graph.py | 836 | DAG 引擎：依赖解析/并行批次/重试/checkpoint/回滚 |
| telemetry.py | 816 | 60 秒周期采集、认知稳定性评分 |
| state_manager.py | - | 状态快照 capture/restore/diff/cleanup |
| recovery_manager.py | 1,032 | 崩溃恢复：健康检查 + 任务恢复 + 会话抢救 |
| event_logger.py | - | NDJSON 追加式事件日志（线程安全，自动轮转 100MB） |
| db_schema.py | - | 3 个 SQLite 库的 Schema 管理（WAL 模式） |
| runtime_integration.py | 1,050 | 桥接层：将所有子系统接入 execute_tool() 热路径 |
| hooks.py | - | 生命周期钩子：session_start/end, tool_call, llm_call |
| tools.py | - | 6 个 LLM 工具 + 2 个 slash 命令的 handler |
| exceptions.py | - | 9 种异常类的继承体系 |
| cli.py | - | 16 个 CLI 命令 (init/status/health/snapshot/...) |
| config/policy.yaml | - | 策略配置：禁止动作、阈值、域规则 |

### 稳定性 → 行为控制 (Homeostasis)

```
稳定性评分 (0-1)
  ≥ 0.7  → normal    正常执行，无限制
  0.5-0.7 → reduced  规划深度-2，并行度-1，节流
  0.3-0.5 → safe     安全模式，禁止自主循环
  < 0.3  → frozen    冻结非关键任务，触发恢复
```

评分公式：`0.3×内存 + 0.2×规划收敛 + 0.2×记忆卫生 + 0.15×恢复 + 0.15×磁盘`

### 漂移防御

DriftAnalyzer 检测到异常时自动触发：
- Memory 膨胀 → prune
- Planner 权重漂移 → reset
- Event 风暴 → throttle EventBus
- Reflection 噪声 → 降低频率
- 系统不稳定 → trigger recovery

---

## 二、hermes-runtime 多 Agent 编排

### 设计哲学

- **Overlay, not fork** — 不改 Hermes 一行代码
- **Contract-first** — 先定协议（状态机、事件 schema），再写实现
- **Evolve from usage** — 不预设需求，从真实跑通一条路径再抽象

### 架构

```
┌──────────────┐
│  profiles/   │  ← Agent 身份定义 (YAML)
└──────┬───────┘
┌──────┴───────┐
│ delegation/  │  ← 编排核心: Supervisor → Worker
└──────┬───────┘
┌──────┴───────┐
│  contracts/  │  ← 协议层: 状态机 / 事件 / 策略
└──────┬───────┘
┌──────┴───────┐
│  telemetry/  │  ← 可观测性: JSONL 事件存储
└──────────────┘
```

### 模块清单 (1,746 行)

| 模块 | 行数 | 职责 |
|------|------|------|
| contracts/task.py | - | Task 状态机：8 种状态、合法转移 DAG、TransitionError 保护 |
| contracts/agent.py | - | AgentProfile + AgentState + 能力匹配 |
| contracts/events.py | - | 20 种事件类型、TelemetryEvent 不可变、TraceContext 调用链 |
| contracts/policy.py | - | 3 种重试策略、3 种升级策略、4 种路由策略 |
| delegation/supervisor.py | - | Worker 注册 / 能力路由 / 重试 / 升级 / 状态查询 |
| delegation/worker.py | - | 3 种 Delegator 后端：Inline / DelegateTask / Subprocess |
| delegation/task_manager.py | - | 优先级堆队列、超时检测、线程安全 |
| telemetry/store.py | - | JSONL append-only 存储、内存索引、按日期轮转 |
| profiles/*.yaml | - | researcher / coder / reviewer 三种 Agent 人格 |

### Soak Test 结果

```
Phase 1  健康检查    3 tasks   ✅   6.03s
Phase 2  串行压力    6 tasks   ✅  44.0s (avg 7.3s)
Phase 3  并行测试    3 tasks   ✅   6.32s
Phase 4  失败场景    3 tests   ✅  145.5s (含 sleep 120)
Phase 5  长时浸泡   10 tasks   ✅  32.3s

总计: 25 次 delegate_task | 失败率 0%
内存峰值: 808MB / 4.8Gi (16.8%) | Swap: 0
```

关键验证点：sleep 120 超时测试在 4GB 环境下触发 OOM 崩溃（exit 137），升级到 5GB 后通过。

---

## 三、Skill Substrate 技能基底

### 问题

原生 Hermes 加载 skill 时，整个 SKILL.md 一次灌入上下文。以 scraping-gateway 为例：
- 改造前：18KB / 410 行（每次加载 100%）
- 改造后：2.8KB 主文件 + 按需加载 1-4KB reference（节省 50-77%）

### 三阶段改造

**P0-1: references/ 拆分**
```
scraping-gateway/
  SKILL.md                   ← 2.8KB 总览 + 索引表
  references/
    l0_static.md             ← L0 静态抓取
    l1_content.md            ← L1 内容提取
    l2_dynamic.md            ← L2 Playwright
    02-l3-service-architecture.md  ← L3 CloakBrowser
    l4_manual.md             ← L4 人工接管
    memory_safety.md         ← WSL OOM 保护
    troubleshooting.md       ← 排错指南
    07-site-policy-summary.md ← 站点策略
```

**P0-2: AGENTS.md 聚合脚本**

`generate_agents.py` 扫描 154 个 skill → 生成 740 行总览。协调 Agent 只读 AGENTS.md（知道"有什么"），Worker 按需加载 SKILL.md + references（知道"怎么做"）。

**P0-3: skill_router + 遥测**

`skill_router.py` 实现完整的 catalog → select → load → reference → execute 链路，9 事件可追溯：

```
skill.catalog.loaded    → 154 skills, AGENTS.md
skill.selected          → scraping-gateway (llm_routed)
skill.loaded            → SKILL.md 2.8KB, 8 references auto-discovered
skill.reference.loaded  → l0_static.md 1.3KB (on-demand)
skill.execution.started → intent recorded
skill.execution.completed → l0 succeeded, 92966 chars, 34s
agent.task.delegated    → spawned subagent
agent.worker.completed  → 3 tool calls
agent.result.returned   → GPT-4 Technical Report
```

### 多 Agent 场景的 Token 节省

| 场景 | 改造前 | 改造后 | 节省 |
|------|--------|--------|------|
| 单 Agent 加载 scraping | 18KB | 4.1KB | 77% |
| Supervisor + 2 Worker | 54KB | 14KB | 74% |
| Supervisor + 4 Worker | 90KB | 22KB | 76% |

---

## 四、12 小时稳定性测试

```
═══════════════════════════════════════════
  HERMES CORE 12-HOUR STABILITY TEST
═══════════════════════════════════════════
  历时:    11.93 小时 (09:31 → 21:27)
  检查:    24/24 ✅
  成功:    24 ✅
  失败:    0 ✅
  异常:    无 ✅
  子系统:  16/16 ACTIVE (24 轮全部通过)
  稳定性:  1.0 满分 (24 轮全部 1.0)
  VERDICT: PASS ✅
═══════════════════════════════════════════
```

### 混沌工程测试 (8 场景全部恢复)

| 故障注入 | 恢复 | 成功 |
|---------|------|------|
| kill_browser | ✅ | ✅ |
| lock_database | ✅ | ✅ |
| random_timeout | ✅ | ✅ |
| disconnect_net | ✅ | ✅ |
| delete_cache | ✅ | ✅ |
| random_failure | ✅ | ✅ |
| memory_pressure | ✅ | ✅ |
| kill_event_log | ✅ | ✅ |

---

## 五、多层抓取架构

```
L0  curl/API           → 简单网页、REST API
L1  解析增强            → trafilatura HTML 解析
L2  无头浏览器          → Playwright / Chromium
L3  隐身浏览器          → CloakBrowser (WSL 原生, 指纹伪装+人类行为模拟)
L4  人工介入            → 需要登录/验证码的站点

自动降级: L0 → L1 → L2 → L3 → L4
每层最多 3 次重试 (Factor 9)
```

L3 层的独特价值：CloakBrowser 基于 Chromium 隐身引擎，每次启动随机化浏览器指纹（fingerprint ID、平台伪装），
模拟人类鼠标/键盘/滚动行为，绕过大部分反爬检测。原生运行在 WSL 中，无需 Windows 中转。
通过系统代理实现出口 IP 隐藏，无需额外配置即获得 IP 保护。

---

## 六、对比总结

| 维度 | 原生 Hermes | 增强后 |
|------|------------|--------|
| Agent 模型 | 单 Agent | Supervisor/Worker 多 Agent |
| 认知层 | 无 | 16 子系统 RuntimeHotPath |
| 稳定性验证 | 无 | 12h 连续测试 + 8 场景混沌 |
| 技能加载 | 全量灌入 | references/ 按需 (省 77%) |
| 可观测性 | 无 | 9 事件遥测链 + JSONL 持久化 |
| 抓取能力 | 无 | 5 层自动降级 + 隐身浏览器 |
| 状态持久化 | 文本记忆 | 4 SQLite + 5 层记忆 |
| 安全约束 | Tirith (可关) | 5 层 PolicyEngine (始终在线) |
| 恢复能力 | 手动 | 自动崩溃检测 + 恢复 |

---

## 已知限制

1. 标准 Hermes 工具（terminal/read_file/write_file）不经过 RuntimeHotPath — 只有 hermes-core 注册的工具走完整管道
2. OODA Loop 已修复但未在真实对话中长时间运行
3. 语义检索仍是 substring 匹配，没有 embedding
4. 认知稳定性评分已采集但未主动驱动自动降级
5. hermes-runtime 的 Reviewer 层未实现
