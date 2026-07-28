---
name: xhs-collector
description: 小红书账号笔记+评论采集工具（v3 全 Edge 架构）。支持两种输入模式：(1) 主页完整 URL（带 xsec_token）+ 日期；(2) 直接给 redId（小红书号）+ 日期，自动通过 CDP+Edge 刷新 token。通过 Playwright 接管真实 Edge 完成 SSR DOM 提取，自动采集账号当天所有笔记/评论/图片并同步到飞书 Wiki「小红书采集数据」多维表格。触发场景：用户说"采集小红书账号 XXX 的 YYYY-MM-DD 笔记"、"按日期采集小红书内容"、"同步小红书数据到飞书"等。已存在记录按 redId/笔记ID/评论ID 去重更新（record-upsert 显式传 record-id）。
agent_created: true
---

# xhs-collector · 小红书采集 Skill (v3)

## v3 架构核心变化（2026-07-28）

**弃用 xiaohongshu-mcp，全部采集通过 Playwright CDP 接管真实 Edge 完成。**

| 项 | v2.1（MCP） | v3（全 Edge） |
|---|---|---|
| 采集引擎 | xiaohongshu-mcp（headless Chromium） | Playwright CDP 接管真实 Edge |
| 指纹隐蔽性 | 差（headless 指纹被识别，触发登录墙） | 好（真实 Edge 指纹） |
| 登录态管理 | MCP 独立维护 cookies.json，易与 Edge 失步 | 一个浏览器 = 一个登录态，无失步 |
| profile/详情数据 | 拦截 API（user/otherbeta / feed_detail） | SSR DOM 提取（数据嵌在 HTML / __INITIAL_STATE__） |
| 评论数据 | API 拦截（comment/page v1） | API 拦截（comment/page **v2**） |
| 风控防御 | 仅开头检查 + 固定间隔 | 三重检查 + 中途复检 + 熔断 + 随机化间隔 |

**v3 触发的关键认知**：小红书 profile 页和笔记详情页都是 **SSR（服务端渲染）**，数据直接嵌在 DOM 里，不发异步 API。详情数据在 `__INITIAL_STATE__.note.noteDetailMap[noteId].note`（Vue3 ref，读 `_rawValue`）。

## Overview

支持**两种输入模式**：

| 模式 | 输入 | 何时用 | 前置条件 |
|---|---|---|---|
| **A. profile-url** | 主页完整 URL（必须带 `xsec_token`） | 用户已手动从浏览器复制 URL | 无 |
| **B. red-id**（推荐） | redId（小红书号，纯数字） | 只知道 redId，或想让 token 自动刷新 | Edge 已用调试端口启动 + 登录小红书账号 |

两种模式都会自动完成：
1. 模式 A：从 URL 解析 `user_id` + `xsec_token`；模式 B：通过 CDP+Edge 搜索 redId，拦截 onebox API 拿 token
2. 三重登录检查（cookie + 页面特征 + user/me API 查 `guest:false`）
3. 采集账号主页信息（粉丝/关注/获赞）—— **SSR DOM 提取**
4. 滚动加载笔记列表 —— **SSR，从 DOM 累积去重**
5. 按日期筛选当天发布的笔记（逐篇拉详情，从 `noteDetailMap` 读时间戳）
6. 采集每篇笔记详情（标题/正文/图片/互动数据/IP/标签）—— **SSR 提取**
7. 采集每篇笔记评论 —— **API 拦截（comment/page v2）**
8. 下载所有图片
9. 同步到飞书 Wiki 多维表格（**record-upsert 显式传 record-id 去重**）

## 🛡️ 风控防御（v3 强化，对应 7-28 风控事件）

### 采集前：三重登录预检
1. `web_session` cookie 存在且足够长
2. explore 页无登录按钮、URL 未跳 `/login`
3. **user/me API 返回 `guest:false`**（关键：cookie 在但服务端判游客 = 无效登录）

### 采集中：中途登录态复检（P0 防御）
- **每篇笔记前** `quick_session_check()`：查 cookie 是否还在/变化，无网络请求
- **每 5 篇** `periodic_login_probe()`：发 user/me API 查 `guest:false`
- 任一失败 → 立即 `sys.exit(3)`，不再继续采集

### 熔断机制（P0 防御）
- 连续 3 次错误 → 立即 `sys.exit(3)`
- 防 session 失效后 continue 撞墙（7-28 事件的核心痛点）

### 随机化间隔（反规律性）
- API 间隔：log-normal 分布，中位数 ~2.5s，钳制 [1.5, 8.0]
- 命中笔记间隔：30-60s 随机
- 每 5 篇小休：60-120s 随机
- 滚动后等待：2.5-5.5s 随机

## 🎯 推荐用 red-id 模式（自动刷新 token）

### 前置条件 1：启动带调试端口的 Edge（一次性）

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\ensure_edge.ps1"
```

**为什么用独立 Edge 实例**：
- 不能用日常 Chrome（用户日常使用，会被打断）
- Playwright 自带 Chromium 会被小红书指纹识别为自动化工具
- CDP 接管真实 Edge = 真实指纹 + 可用登录态

### 前置条件 2：扫码登录小红书账号（首次一次）

在弹出的 Edge 窗口里扫码登录。cookies 持久化到 `--user-data-dir`，之后每次启动自动加载。

### 之后只需告诉 skill redId + 日期

```
帮我采集小红书号 2228145708 在 2026-07-29 的笔记
```

skill 会自动：
1. 用 Playwright 接管已启动的 Edge
2. 在 Edge 里搜索 redId，拦截 onebox API 拿 `user_id` + `xsec_token`
3. 继续正常的采集流程

## 前置依赖

### 1. (核心) Edge 调试端口已启动 + 登录小红书账号
见上面「前置条件」一节。

### 2. 飞书连接器已启用
WorkBuddy 已连接飞书，lark-cli 可直接调用。

### 3. Python 依赖
- `playwright`（Playwright Python 包）
- Node.js（lark_run.js 包装脚本用，解决 Windows GBK 编码问题）

## 执行流程

### Step 1: 检查环境
- curl 测试 `http://127.0.0.1:9222/json/version` 是否可达
- 如果 Edge 没起，提示用户运行 `ensure_edge.ps1`

### Step 2: 运行采集脚本

**模式 A（profile-url）**：
```bash
python ".../collect_v3.py" --profile-url "<URL>" --date <YYYY-MM-DD> --out ./xhs_output
```

**模式 B（red-id，推荐）**：
```bash
python ".../collect_v3.py" --red-id <redId> --date <YYYY-MM-DD> --out ./xhs_output
```

输出：
- `xhs_output/<redId>_<nickname>/<redId>_<nickname>_<date>.json`
- `xhs_output/<redId>_<nickname>/images/<note_id>_<idx>.webp`

### Step 3: 同步到飞书
```bash
python ".../sync_to_lark.py" --input ./xhs_output/<...>/<...>.json --data-root ./xhs_output
```

**去重机制（v3 修复）**：`record-upsert` 不带 `--record-id` 时永远创建新记录（不按业务键去重）。代码里先 `find_record` 拿到 existing record-id，再显式传 `--record-id` 才会更新。

同步完成后给出飞书 URL：https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb

### Step 4: 报告结果
- 采集到几篇笔记、几张图、几条评论
- 飞书表格里新增/更新了多少条记录

## 输入解析

### redId 识别
- 纯数字 `2228145708`
- 加文字 `小红书号 2228145708` / `redId 2228145708`
- 校验：纯数字，8-11 位

### 主页 URL 识别
- URL path 形如 `/user/profile/<24位hex>`
- URL query 必须有 `xsec_token`

### 日期解析
- 标准 `YYYY-MM-DD`
- 中文 `2026年7月29日` → `2026-07-29`
- 相对 `昨天`、`前天` —— 用 `date` 命令算北京时间

时区固定北京时间（UTC+8），按笔记 `time` 毫秒时间戳 +8 小时判断日期。

## 已知限制

| 限制 | 说明 |
|---|---|
| **Edge 必须运行 + 登录** | Edge 用 `--remote-debugging-port=9222` 启动，登录小红书账号 |
| **xsec_token 有时效** | 模式 A 的 URL 通常几小时到几天失效；模式 B 每次拿新的 |
| **SSR 数据无 API** | profile/详情是 SSR，数据在 DOM / `__INITIAL_STATE__`，不在 API 响应里 |
| **按天过滤慢** | feeds 列表无时间字段，必须逐篇拉详情（每篇间隔 1.5-8s 随机，命中笔记 30-60s） |
| **评论上限** | 每笔记首页评论，不主动展开二级回复 |
| **Windows GBK 编码** | sync_to_lark 通过 `lark_run.js`（Node.js CreateProcessW）包装，避开 Python subprocess 的 GBK 限制 |

## 定时任务（scheduler.py + automation）

- 触发：每周二/四/六凌晨 5:00（WorkBuddy automation）
- 启动随机抖动：1-30 分钟
- 账号间隔：30-90 分钟（多账号时，单账号不触发）
- 失败策略：遇错即停，exit code 3 = 登录态失效/熔断

## 资源索引

### scripts/
- `collect_v3.py` —— **主采集脚本 v3**（全 Edge CDP 架构，SSR DOM 提取）
- `lark_run.js` —— Node.js 包装脚本（解决 Windows GBK 编码）
- `sync_to_lark.py` —— 飞书同步脚本（record-id 去重 + lark_run.js 包装）
- `scheduler.py` —— 定时调度脚本 v3（三重预检 + 中途复检触发）
- `refresh_token.py` —— redId → xsec_token 刷新（旧版，v3 已集成进 collect_v3）
- `ensure_edge.ps1` —— 启动带 CDP 的 Edge 实例
- `collect.py` —— **旧版 v2.1 采集脚本（已废弃，保留备查）**
- `llm_infer_product.py` / `writeback_llm_results.py` —— 推测带货品类 LLM 推断工具
- `backfill_product_category.py` —— 历史笔记回填品类

### references/
- `lark_schema.md` —— 飞书多维表格 schema
- `risk_control.md` —— 风控规则（旧版，v3 风控逻辑见本文件）

## 示例对话

**示例：red-id 模式（推荐）**

**用户**：帮我采集小红书号 2228145708 在 2026-07-29 的笔记

**助手**：好的，开始采集 redId=2228145708 在 2026-07-29 的笔记。
1. 检查 Edge CDP（9222）状态...
2. 三重登录检查：cookie OK + 页面特征 OK + user/me guest=False ✅
3. 通过 CDP+Edge 刷新 token：上岸奋斗, fans=1439
4. 导航主页，SSR DOM 提取账号信息 + 笔记列表（30 篇）
5. 滚动加载（5 次），合并去重 180 篇笔记元数据
6. 逐篇拉详情过滤 7-29：命中 3 篇（含中途 session 复检 + 周期性探测）
7. 同步到飞书 Wiki（record-id 去重，已有则更新）

✅ 完成。采集 3 篇笔记、3 张图、N 条评论。
