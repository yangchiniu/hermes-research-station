# Changelog

## 2026-05-29: Phase 2 — 4 激活模块 + 2 项质量修复

### 背景

hermes-core 有 4 个子系统一直在初始化但未激活（Telemetry、Watchdog、Goal Manager、Drift Analyzer），以及 Planner 的 LLM 分解功能存在生产环境断路问题。本次完成激活 + 修复。

### 核心变更

**模块激活（4/4）**
- Telemetry: 60s 守护线程采集运行时指标 ✅
- Watchdog: 每次工具调用发送心跳 + 死锁检测 ✅
- Goal Manager: 自动追踪唯一工具调用并标记完成 ✅
- Drift Analyzer: 保持 10% 采样激活 ✅
- 所有模块通过 RuntimeHotPath._init_subsystems() 在启动时统一激活

**Planner LLM 分解修复（P0）**
- 问题: `_llm_decompose_goal()` 使用裸 `urllib.request` 直调 API，不走 Hermes 配置、不走代理（Clash）、无重试
- 修复:
  - 读取 `~/.hermes/config.yaml` 获取模型端点（`model.base_url` + `model.default`）
  - `ProxyHandler` 自动尊重 HTTP_PROXY/HTTPS_PROXY 环境变量
  - 3 次重试 + 指数退避（1s, 2s, 最终失败）
  - 保留环境变量回退（向后兼容）
- 效果: 从"硬编码断路"变为"config.yaml 同步 + 代理 + 重试"

**Goal 自动追踪修复（P1）**
- 问题: 匹配使用 `endswith(tool_name)` — 子串误匹配风险；遍历全部 goal 的 O(n) 复杂度
- 修复: 模块级 `_GOAL_TOOL_INDEX` 反向索引 → 精确 O(1) 匹配
- 额外 bug 修复: `register_goal(..., domain="tool")` 参数不合法（`register_goal` 不接受 `domain`），整段代码一直在静默 TypeError。改为 `context={"domain": "tool"}`，首次真正生效

### 项目评分

| 维度 | 之前 | 之后 |
|------|------|------|
| 架构设计 | 9 | 9 |
| 代码质量 | 8 | 8 |
| 模块激活 | 6 | 8 |
| 运行时健壮性 | 7 | 8 |
| 测试成熟度 | 7 | 7 |
| 集成就绪度 | 7 | 7 |
| 功能完整度 | 8 | 8 |
| **总分** | **7.4** | **7.9** |

### 验证

- 56/56 测试通过（0 failed, 0 skipped）
- 实际推理: config.yaml 正确读取、ProxyHandler 配置、无 key 优雅回退均验证

---

## 2026-05-19: L3 引擎迁移 — Edge CDP → CloakBrowser

### 背景

L3 抓取层原方案依赖 Windows Edge 浏览器的 CDP (Chrome DevTools Protocol) 远程调试接口。
该方案需要 Windows 侧启动 Edge + Python 中转服务，维护复杂度高，且受限于 WSL-Windows 跨层通信。

本次将 L3 引擎替换为 CloakBrowser——一个基于 Chromium 的隐身浏览器 Python 库，原生运行在 WSL 中，
无需 Windows 中转。API 接口保持不变，Gateway 无缝切换。

### 核心变更

**引擎替换**
- 旧: Edge CDP (端口 19222) + Windows Python 中转
- 新: CloakBrowser (WSL 原生, ~/.venvs/cloak/) + FastAPI (端口 19223)

**修复问题**
- Playwright Sync API 与 asyncio 冲突: health 端点改为不触发浏览器初始化
- fastapi/uvicorn/pydantic 依赖安装到 CloakBrowser venv
- Gateway 层更新: docstring、错误信息、extracted_data 传递

**API 兼容性**
- POST /scrape {url, wait_seconds, extraction_prompt} — 不变
- GET /health, GET /status — 不变
- ScrapeResponse 结构 — 不变 + 新增 extracted_data 字段

### CloakBrowser 反检测能力

| 能力 | 状态 |
|------|------|
| 指纹随机化 | ✅ 每次启动随机 fingerprint ID |
| 平台伪装 | ✅ --fingerprint-platform=windows |
| WebRTC 防泄漏 | ✅ 自动匹配代理出口 IP |
| GeoIP 适配 | ✅ 根据代理 IP 设置时区/语言 |
| 人类行为模拟 | ✅ 鼠标/键盘/滚动 (humanize) |
| 代理支持 | ✅ HTTP/SOCKS5 + 认证 |
| 代理轮换池 | ❌ 需外部提供 |
| 验证码绕过 | ❌ 滑块验证码无法绕过 |

### 10 站真实采集评测

| ID | 站点 | 得分 | 说明 |
|----|------|------|------|
| 1 | 知网 | 25 | 滑动验证码 |
| 2 | 万方 | 55 | 7221字节真实内容 |
| 3 | 维普 | 45 | 14112字节，触发验证但返回内容 |
| 4 | 国家统计局 | 45 | 403封禁(VPS IP被WAF拦截) |
| 5 | 知乎 | 55 | 通过反爬，需登录态 |
| 6 | DOAJ | 40 | SPA动态加载不完整 |
| 7 | 百度学术 | 25 | 安全验证 |
| 8 | arXiv | 55 | 20705字节，最佳 |
| 9 | 超星 | 45 | 404(URL格式变更) |
| 10 | NSFC基金委 | 65 | 最高分 |

综合: 455/850 (54%) | HTTP成功率 10/10 | 有效内容 4/10 | 评级 B+

---

## 2026-05-18: hermes-core 子系统接入 — 7 项已知限制修复

### 背景

第一轮审计修复了 7 个数据流 bug（见下方条目）。本轮修复 7 个已知限制，
将之前"初始化但未接入"的子系统连接到实际执行流，让学习循环从"只写不读"变为"读写闭环"。

### 修复清单

- **Fix #1**: PolicyEngine fail-open → fail-closed (安全)
- **Fix #2**: 记忆检索结果影响学习循环 (数据流)
- **Fix #3**: Watchdog/TaskGraph/SelfObservation 接入执行流 (架构)
- **Fix #4**: EventBus handler 从 no-op 变为实际处理 (事件驱动)
- **Fix #5**: 清理 bare except: pass — 38处改为 logger.debug (可维护性)
- **Fix #6**: 反思/经验截断长度提升 300→1000 / 200→500 (数据质量)
- **Fix #7**: Planner 从 ExperienceManager 学习 (学习闭环)

### 额外修复

- DriftAnalyzer API 修复: `drift.observe()` → `drift.get_summary()` / `drift.analyze_all()`

### 资源影响

| 项目 | 开销 |
|------|------|
| 额外 LLM 调用 | 0 |
| CPU 增量 | < 100ms / 工具调用 |
| 内存增量 | < 2MB |
