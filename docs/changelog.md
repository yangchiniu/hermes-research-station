# Changelog

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
