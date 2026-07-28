# xhs-collector-skill

小红书账号笔记+评论采集 WorkBuddy Skill（**v3 - 全 Edge 架构**）。

## ⚡ v3 重要变化（2026-07-28）

> **弃用 xiaohongshu-mcp，全部采集通过 Playwright CDP 接管真实 Edge 完成。**

v2.x 依赖��� xiaohongshu-mcp 使用 headless Chromium，指纹被小红书识别后触发登录墙。v3 改为接管真实 Edge 浏览器（真实指纹 + 真实登录态），并发现 profile/详情页都是 **SSR（服务端渲染）**，数据直接嵌在 DOM 里，不再依赖 API 拦截。

| 维度 | v2.x（MCP） | v3（全 Edge，当前） |
|---|---|---|
| 采集引擎 | xiaohongshu-mcp（headless Chromium） | Playwright CDP 接管真实 Edge |
| 指纹隐蔽性 | 差（被识别为自动化） | 好（真实 Edge 指纹） |
| 登录态 | MCP 独立维护 cookies.json，易失步 | 一个浏览器 = 一个登录态 |
| profile/详情 | 拦截 API | **SSR DOM 提取**（数据在 DOM / `__INITIAL_STATE__`） |
| 评论 | API v1 拦截 | API **v2** 拦截 |
| 风控防御 | 开头检查 + 固定间隔 | 三重检查 + 中途复检 + 熔断 + 随机化 |
| 去重 | upsert 不带 record-id（会��建重复） | **显式传 record-id 去重** |

## 功能

支持**两种输入模式**：

| 模式 | 输入 | 何时用 | 前置条件 |
|---|---|---|---|
| **A. red-id**（推荐） | redId（小红书号）+ 日期 | 只知道 redId | Edge 已启动 + 登录 |
| **B. profile-url** | 主页完整 URL（带 `xsec_token`）+ 日期 | 已手动复制 URL | 无 |

两种模式都会自动：
1. 三重登录检查（cookie + 页面特征 + user/me API 查 `guest:false`）
2. 采集账号主页信息（SSR DOM 提取粉丝/关注/获赞）
3. 滚动加载笔记列表（SSR 累积去重）
4. 按日期筛选当天笔记（逐篇拉详情，从 `noteDetailMap` 读时间戳）
5. 采集笔记详情（SSR 提取标题/正文/图片/互动/IP/标签）
6. 采集评论（API v2 拦截）
7. 同步飞书（record-id 去重）

## 前置依赖

### 1. Edge 浏览器带 CDP 调试端口 + 已登录小红书

```powershell
# 推荐：用 skill 自带的启动脚本
powershell -ExecutionPolicy Bypass -File scripts/ensure_edge.ps1
```

首次启动后在弹出的 Edge 窗口扫码登录小红书账号，cookies 持久化到 `--user-data-dir`，之后自动加载。

**为什么不能用 Chrome 或 Playwright Chromium**：
- Chrome 是用户日常浏览器，不能干扰
- Playwright Chromium 会被小红书指纹识别为自动化工具，触发登录墙
- CDP 接管真实 Edge = 真实指纹 + 真实登录态

### 2. 飞书连接器

WorkBuddy 已连接飞书，`lark-cli` 可直接调用。如要复用此 skill，修改 `scripts/sync_to_lark.py` 里的 `BASE_TOKEN` / `TBL_*` 常量。schema 见 [`references/lark_schema.md`](references/lark_schema.md)。

### 3. Python 依赖

- `playwright`（Playwright Python 包）
- Node.js（`lark_run.js` 包装脚本用，解决 Windows GBK 编码）

## 用法

### 单次采集（red-id 模式，推荐）

```bash
# Step 1: 确保 Edge CDP 已启动 + 已登录
powershell -ExecutionPolicy Bypass -File scripts/ensure_edge.ps1

# Step 2: 采集（只需 redId + 日期）
python scripts/collect_v3.py --red-id 2228145708 --date 2026-07-29 --out ./xhs_output

# Step 3: 同步飞书
python scripts/sync_to_lark.py \
  --input ./xhs_output/<redId>_<nickname>/<redId>_<nickname>_2026-07-29.json \
  --data-root ./xhs_output
```

在 WorkBuddy 里直接用自然语言触发：
> 帮我采集小红书号 2228145708 在 2026-07-29 的笔记

## 🛡️ 风控防御（v3 强化）

### 采集前：三重登录预检
1. `web_session` cookie 存在且足够长
2. explore 页无登录按钮、URL 未跳 `/login`
3. **user/me API 返回 `guest:false`**（关键）

### 采集中：中途复检（对应 7-28 风控事件）
- 每篇笔记前 `quick_session_check()`（查 cookie，无请求）
- 每 5 篇 `periodic_login_probe()`（发 user/me 查 guest）
- 连续 3 次错误立即 `sys.exit(3)` 熔断

### 随机化间隔
- API 间隔：log-normal 分布（中位数 ~2.5s，钳制 [1.5, 8.0]）
- 命中笔记：30-60s 随机
- 每 5 篇小休：60-120s 随机

## 定时任务（Automation）

- **频率**：每周二/四/六凌晨 5:00 触发 + 1-30 分钟随机抖动
- **预检**：三重登录检查，未登录直接退出不请求
- **采集**：collect_v3.py + sync_to_lark.py
- **熔断**：遇错即停，exit 3 = 登录态失效/风控
- **账号间隔**：30-90 分钟（多账号时，跨小时降批量特征）

详细配置见 [`AUTOMATION.md`](AUTOMATION.md)。

## 目录结构

```
xhs-collector-skill/
├── SKILL.md                       # Skill 主入口（WorkBuddy 读取）
├── README.md                      # 本文件
├── AUTOMATION.md                  # 定时任务配置文档
├── scripts/
│   ├── collect_v3.py              # 主采集脚本 v3（全 Edge CDP，SSR DOM 提取）
│   ├── lark_run.js                # Node.js 包装（解决 Windows GBK 中文 JSON）
│   ├── sync_to_lark.py            # 飞书同步（record-id 去重）
│   ├── scheduler.py               # 定时调度 v3（三重预检 + 中途复检触发）
│   ├── refresh_token.py           # redId → xsec_token 刷新（旧版，已集成进 v3）
│   ├── ensure_edge.ps1            # 启动带 CDP 的 Edge（含登录态持久化）
│   ├── collect.py                 # 旧版 v2.1 采集（已废弃，保留备查）
│   ├── llm_infer_product.py       # 推测带货品类 LLM 推断
│   ├── writeback_llm_results.py   # LLM 结果批量回填飞书
│   └── backfill_product_category.py  # 历史笔记回填品类
└── references/
    ├── lark_schema.md             # 飞书多维表格 schema
    ├── risk_control.md            # 风控规则（v3 新增防御见 SKILL.md）
    └── xhs_mcp_reference.md       # xiaohongshu-mcp 文档（已废弃，留档）
```

## 已知限制

- **Edge 必须运行 + 登录**：Edge 用 `--remote-debugging-port=9222` 启动，登录小红书账号
- **xsec_token 有时效**：模式 B 的 URL 几小时到几天失效；模式 A 每次拿新的
- **SSR 数据无 API**：profile/详情是 SSR，数据在 DOM / `__INITIAL_STATE__`，不在 API 响应
- **按天过滤慢**：feeds 列表无时间字段，必须逐篇拉详情
- **评论上限**：每笔记首页评论，不展开二级回复
- **Windows GBK 编码**：sync_to_lark 通过 `lark_run.js`（Node.js CreateProcessW）包装避开

## License

MIT
