# Hermes Research Station

面向理工科研究生的 AI 科研工作站。基于 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 构建，专注于科研场景的数据采集、知识管理和工作流自动化。

> 不重复造轮子。在成熟的 Agent 框架之上，做面向科研的应用层价值。

---

## 愿景

**短期（当前）**：稳定核心 + 数据采集能力
- Hermes Agent 运行稳定性（WSL2 环境，内存优化，长期运行无 OOM）
- 多层网页抓取架构（L0-L4），覆盖从简单 API 到重度反爬站点
- 学术文献检索与聚合（OpenAlex、arXiv、CNKI/知网）

**中期**：个人科研助手 + 跨终端
- 自然语言驱动的文献调研 → 结构化综述生成
- 跨终端交互：CLI、即时通讯、移动端
- 探索 AI 眼镜等新形态终端的实时科研辅助场景

**长期**：面向 AI 时代的基础科研设施
- 将工作流打包为可复现的科研工具链
- 为理工科研究生提供开箱即用的 AI 辅助科研环境
- 持续迭代，成为 AI-native 科研基础设施

---

## 技术架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户交互层                              │
│  CLI ─── 即时通讯 Bot ─── Cron 定时任务 ─── Webhook      │
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
│                  数据采集层                                │
│                                                          │
│  L0  curl/API         → 简单网页、REST API               │
│  L1  解析增强          → HTML 解析、JSON 提取              │
│  L2  无头浏览器        → Playwright/Chromium               │
│  L3  真实浏览器        → Edge CDP (Windows 侧)            │
│  L4  人工介入          → 需要登录/验证码的站点              │
│                                                          │
│  自动降级: L0 → L1 → L2 → L3 → L4                       │
│  失败重试: 每层最多 3 次                                   │
└─────────────────────────────────────────────────────────┘
```

### 核心模块

| 模块 | 职责 | 关键设计 |
|------|------|----------|
| **Skill System** | 可扩展的能力插件 | 150+ 技能覆盖 MLOps、文献检索、网页抓取、代码开发等 |
| **hermes-core 插件** | 认知架构 | OODA 决策循环、任务规划器、事件溯源、运行时遥测 |
| **Scraping Gateway** | 多层抓取 | 5 层自动降级 + 重试策略 + 质量评估 |
| **Delegation** | 子 Agent 编排 | delegate_task 并行执行、资源感知调度 |
| **Memory** | 持久化记忆 | 跨会话记忆、技能库、会话搜索 |
| **LLM Provider** | 模型接入 | 多模型切换（DeepSeek、MiMo 等），Token Plan 支持 |

### 技术栈

- **Runtime**: Python 3.12, WSL2 (Windows Subsystem for Linux)
- **Agent Framework**: Hermes Agent v0.13+
- **LLM**: Xiaomi MiMo v2.5 Pro (Token Plan), DeepSeek v4 Flash
- **Scraping**: Playwright, aiohttp, Edge CDP
- **Data**: SQLite, JSONL 事件日志
- **Service**: systemd (gateway), cron (定时任务)

---

## 项目结构

```
hermes-research-station/
├── README.md              ← 你在这里
├── docs/
│   ├── vision.md          ← 详细愿景与规划
│   └── architecture.md    ← 技术架构详解
├── skills/                ← 自定义科研技能
│   └── ...
├── scripts/               ← 工具脚本
│   └── ...
└── examples/              ← 使用示例
    └── ...
```

---

## 当前状态

- [x] Hermes Agent 核心运行稳定
- [x] 多层抓取架构 L0-L3 已实现
- [x] 认知插件 hermes-core 16/16 子系统加载
- [x] 子 Agent 编排 soak test 通过（25 tasks, 0 failure）
- [x] MiMo v2.5 Pro Token Plan 接入
- [ ] L3 extraction_prompt 对接 WSL 侧
- [ ] 抓取质量 Judge 模式
- [ ] 文献调研自动化工作流
- [ ] 跨终端交互（移动端/新形态终端）

---

## License

MIT
