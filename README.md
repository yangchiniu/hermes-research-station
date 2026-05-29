# Hermes Research Station

面向理工科研究生的 AI 科研工作站。基于 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 构建，专注于科研场景的数据采集、知识管理和工作流自动化。

> 不重复造轮子。在成熟的 Agent 框架之上，做面向科研的应用层价值。

---

## 愿景

**短期（当前）**：稳定核心 + 数据采集能力
- 多层网页抓取架构（L0-L4），覆盖从简单 API 到重度反爬站点
- 学术文献检索与聚合（OpenAlex、arXiv、CNKI/知网）
- WSL2 环境长期运行稳定性

**中期**：个人科研助手 + 跨终端
- 自然语言驱动的文献调研 → 结构化综述生成
- 跨终端交互：CLI、即时通讯、移动端

**长期**：面向 AI 时代的基础科研设施
- 将工作流打包为可复现的科研工具链
- 成为 AI-native 科研基础设施

---

## 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户交互层                              │
│  CLI ─── QQ Bot ─── Cron 定时任务 ─── Webhook           │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                  Hermes Agent 核心                        │
│  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌────────────┐  │
│  │ Skill   │ │ Memory   │ │ Session   │ │ Delegation │  │
│  │ System  │ │ (持久化)  │ │ Manager   │ │ (子Agent)  │  │
│  │ 150+技能 │ │          │ │           │ │            │  │
│  └─────────┘ └──────────┘ └───────────┘ └────────────┘  │
│  ┌──────────────────────────────────────────────────────┐│
│  │              hermes-core 认知插件                      ││
│  │  Planner ─ OODA Loop ─ Kernel ─ FieldRunner          ││
│  │  EventBus ─ Telemetry ─ Reflection ─ Recovery        ││
│  └──────────────────────────────────────────────────────┘│
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                  数据采集层 (Scraping Gateway)             │
│                                                          │
│  L0  curl/API         → 简单网页、REST API               │
│  L1  解析增强          → trafilatura 内容提取             │
│  L2  无头浏览器        → crawl4ai + Playwright            │
│  L3  隐身浏览器        → CloakBrowser (WSL原生)           │
│  L4  人工介入          → 需要登录/验证码的站点              │
│                                                          │
│  自动降级: L0 → L1 → L2 → L3 → L4                       │
│  质量评估: 每层 Judge 评分 (0.0-1.0)                     │
└─────────────────────────────────────────────────────────┘
```

### 核心模块

| 模块 | 职责 | 关键设计 |
|------|------|----------|
| **Skill System** | 可扩展的能力插件 | 150+ 技能覆盖 MLOps、文献检索、网页抓取等 |
| **hermes-core 插件** | 认知架构 | OODA 决策循环、任务规划器、事件溯源、运行时遥测、Watchdog 心跳、Goal 追踪、Drift 检测 |
| **Scraping Gateway** | 多层抓取 | 5 层自动降级 + 质量评估 + 站点策略 |
| **L3 CloakBrowser** | 隐身浏览器 | Chromium 指纹伪装 + 人类行为模拟 + 代理出口 |
| **Delegation** | 子 Agent 编排 | delegate_task 并行执行、资源感知调度 |
| **Memory** | 持久化记忆 | 跨会话记忆、技能库、会话搜索 |

### 技术栈

- **Runtime**: Python 3.12, WSL2 (Windows Subsystem for Linux)
- **Agent Framework**: Hermes Agent v0.13+
- **LLM**: Xiaomi MiMo v2.5 Pro (Token Plan), DeepSeek v4 Flash
- **Scraping**: CloakBrowser (Chromium 146), crawl4ai, trafilatura
- **Data**: SQLite, JSONL 事件日志
- **Service**: systemd (gateway), cron (定时任务)

---

## 项目结构

```
hermes-research-station/
├── README.md              ← 你在这里
├── data/
│   └── cas_oa_journals.json  ← 中科院OA期刊目录 (123本)
├── docs/
│   ├── vision.md          ← 详细愿景与规划
│   ├── architecture.md    ← 技术架构详解
│   └── changelog.md       ← 变更记录
└── plugins/
    └── hermes-core/       ← 认知插件 (hooks/tools/runtime_integration)
```

---

## 当前状态

**已实现**
- [x] Hermes Agent 核心运行稳定 (WSL2, 12h+ soak test)
- [x] 多层抓取架构 L0-L3 已实现
- [x] L3 引擎从 Edge CDP 迁移到 CloakBrowser (WSL原生)
- [x] 认知插件 hermes-core 28 子系统全部加载 + 4 模块已激活 (Telemetry/Watchdog/Goal/Drift)
- [x] Planner LLM 分解支持 config.yaml + 代理 + 重试 (P0 修复)
- [x] Goal 自动追踪 O(1) 精确匹配 + 反向索引 (P1 修复)
- [x] 子 Agent 编排 soak test 通过（25 tasks, 0 failure）
- [x] MiMo v2.5 Pro Token Plan 接入
- [x] QQ Bot 即时通讯交互
- [x] 10站真实采集评测 (B+ 评级)
- [x] 项目评分 7.9/10 (架构9 代码8 激活8 健壮8 测试7 集成7 完整度8)

**进行中**
- [ ] L3 结构化提取器适配各站点 HTML
- [ ] SQLite 线程安全改造 (check_same_thread)

**规划中**
- [ ] 文献调研自动化工作流
- [ ] 跨终端交互（移动端/新形态终端）

---

## 数据采集能力

### L3 CloakBrowser 评测 (2026-05-19)

10个真实学术/数据站点采集评测：

| 站点 | 得分 | 状态 |
|------|------|------|
| 知网 | 25/85 | 滑动验证码（需图书馆代理） |
| 万方 | 55/85 | ✅ 7221字节真实内容 |
| 维普 | 45/85 | ✅ 14112字节，触发验证但返回内容 |
| 国家统计局 | 45/85 | 403封禁（代理IP被WAF拦截） |
| 知乎 | 55/85 | ✅ 通过反爬（需登录态） |
| DOAJ | 40/85 | ✅ SPA动态加载 |
| 百度学术 | 25/85 | 安全验证 |
| arXiv | 55/85 | ✅ 20705字节，最佳 |
| 超星 | 45/85 | 404（URL格式变更） |
| NSFC基金委 | 65/85 | ✅ 最高分 |

**综合评分**: 455/850 (54%) | 成功率 10/10 | 有效内容 4/10


---

## License

MIT
