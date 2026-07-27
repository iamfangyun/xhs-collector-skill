---
name: xhs-collector
description: 小红书账号笔记+评论采集工具。支持两种输入模式：(1) 主页完整 URL（带 xsec_token）+ 日期；(2) 直接给 redId（小红书号）+ 日期，自动通过 CDP+Edge 刷新 token。自动采集账号当天所有笔记/评论/图片并同步到飞书 Wiki「小红书采集数据」多维表格。触发场景：用户说"采集小红书账号 XXX 的 YYYY-MM-DD 笔记"、"按日期采集小红书内容"、"同步小红书数据到飞书"等。已存在记录按 redId/笔记ID/评论ID 去重更新。
agent_created: true
---

# xhs-collector · 小红书采集 Skill (v2.1)

## Overview

支持**两种输入模式**：

| 模式 | 输入 | 何时用 | 前置条件 |
|---|---|---|---|
| **A. profile-url** | 主页完整 URL（必须带 `xsec_token`） | 用户已经手动从浏览器复制了 URL | 无 |
| **B. red-id**（推荐） | redId（小红书号，纯数字） | 只知道 redId，或想让 token 自动刷新 | Edge 已用调试端口启动 + 登录小土豆炒股 |

两种模式都会自动完成：
1. 模式 A：从 URL 解析 `user_id` + `xsec_token`；模式 B：自动调用 `refresh_token.py` 通过 CDP+Edge 刷新拿到
2. 调用本地 xiaohongshu-mcp 采集账号主页信息（粉丝/关注/获赞）
3. 按日期筛选当天发布的所有笔记（必须逐篇拉详情才有时间字段）
4. 采集每篇笔记详情（标题/正文/图片/视频/互动数据/IP/标签）
5. 采集每篇笔记评论（首页最多 10 条一级评论）
6. 下载所有图片到本地缓存目录
7. 同步到飞书 Wiki「小红书采集数据」多维表格（4 张表：账号/笔记/评论/商品）
8. **去重规则**：按 redId / 笔记ID / 评论ID 查现有记录，存在则更新，不存在则新增
9. **推测带货品类**：基于笔记标题+正文+话题标签做关键词匹配，输出 10 大类目之一（金融理财/知识付费/数码电器/美妆个护/服饰鞋包/食品保健/母婴玩具/家居生活/运动户外/旅游服务），推测不出来如实填「未识别」。详细规则见 `scripts/collect.py` 里的 `PRODUCT_CATEGORIES` 列表。

商品官方数据不主动采集（小红书网页版不展示带货内容，平台限制），但笔记表里有「推测带货品类」字段——基于笔记内容关键词做可解释推测，仅供运营参考。

## 🎯 推荐用 red-id 模式（自动刷新 token）

只要满足两个前置条件，之后用户再也不用手动复制 URL：

### 前置条件 1：启动带调试端口的 Edge（一次性）

```powershell
# 用 skill 自带的启动脚本（推荐）
powershell -ExecutionPolicy Bypass -File "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\ensure_edge.ps1"
```

或手动启动：
```bash
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="C:\Users\Administrator\AppData\Local\Temp\edge_crawler_data" \
  --no-first-run --no-default-browser-check \
  https://www.xiaohongshu.com/explore
```

**为什么要用独立 Edge 实例**：
- 不能用日常 Chrome（用户日常使用，会被打断）
- 用 Playwright 自带的 Chromium 会被小红书指纹识别为自动化工具，触发登录墙
- CDP 接管真实 Edge 浏览器 = 真实指纹 + 可用登录态

### 前置条件 2：扫码登录小土豆炒股（首次一次）

在弹出的 Edge 窗口里扫码登录账号「小土豆炒股」（redId: 49274070882）。cookies 会持久化到 `--user-data-dir`，**之后每次启动都会自动加载登录状态**，不需要再扫码。

### 之后只需告诉 skill redId + 日期

```
帮我采集小红书号 2228145708 在 2026-07-26 的笔记
```

skill 会自动：
1. 用 Playwright 接管已启动的 Edge
2. 在 Edge 里搜索 redId
3. 拦截 onebox API 拿到 `user_id` + 最新的 `xsec_token`
4. 继续正常的采集流程

## 模式 A（手动 URL）适用场景

- 用户已经在浏览器里打开了目标账号主页，愿意复制 URL
- 目标账号不是自己的，不想让 Edge 长期登录自己的小号
- Edge 没启动或登录态失效时，临时降级

获取 URL 方法：
1. 在小红书网页端（`www.xiaohongshu.com`）打开目标账号主页
2. 复制地址栏完整 URL，形如：
   ```
   https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=YBPe7A1YMYgnqkfmmffAmoAtzBFc5cSFIcVNi_DKzdOd8%3D&xsec_source=pc_feed
   ```
3. 整段 URL 提供给 skill

## 前置依赖

### 1. xiaohongshu-mcp 服务运行中
```bash
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
./xiaohongshu-mcp-windows-amd64.exe -port ":18060"
```
监听 `http://localhost:18060/mcp`，详细启动和登录流程见 `references/xhs_mcp_reference.md`。

### 2. cookies.json 已登录（xiaohongshu-mcp 用）
首次或 cookie 过期时，单独运行 `xiaohongshu-login-windows-amd64.exe` 扫码登录。当前登录账号：小土豆炒股（redId: 49274070882）。

### 3. (仅 red-id 模式需要) Edge 调试端口已启动 + 登录小土豆炒股
见上面「前置条件」一节。

### 4. 飞书连接器已启用
WorkBuddy 已连接飞书（用户身份 ou_83159d86de092382962bd7fc86665c82），lark-cli 可直接调用。

## 执行流程

收到用户的采集请求后，按以下步骤执行：

### Step 1: 检查环境
- 用 curl 测试 `http://localhost:18060/mcp` 是否可达
- 如果用 red-id 模式，curl 测试 `http://127.0.0.1:9222/json/version` 是否可达
- 如果哪个服务没起，提示用户启动

### Step 2: 运行采集脚本

**模式 A（profile-url）**：
```bash
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\collect.py" \
  --profile-url "<用户提供的完整主页URL>" \
  --date <用户提供的YYYY-MM-DD> \
  --out ./xhs_output
```

**模式 B（red-id）**：
```bash
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\collect.py" \
  --red-id <用户提供的redId> \
  --date <用户提供的YYYY-MM-DD> \
  --out ./xhs_output
```

输出：
- `xhs_output/<redId>_<nickname>/<redId>_<nickname>_<date>.json` —— 完整采集结果
- `xhs_output/<redId>_<nickname>/images/<note_id>_<idx>.webp` —— 每篇笔记的图片

### Step 3: 同步到飞书
```bash
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\sync_to_lark.py" \
  --input ./xhs_output/<redId>_<nickname>/<redId>_<nickname>_<date>.json \
  --data-root ./xhs_output
```

同步完成后给出飞书 URL：https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb

### Step 4: 报告结果
告诉用户：
- 采集到几篇笔记、几张图、几条评论
- 飞书表格里新增/更新了多少条记录
- 如果用了 red-id 自动刷新，告诉用户 token 已更新

## 输入解析

### redId 识别
用户可能提供的格式：
- 纯数字 `2228145708`
- 加文字 `小红书号 2228145708`
- 加文字 `redId 2228145708`

**校验条件**：纯数字，8-11 位

### 主页 URL 识别
用户可能提供的格式：
- 完整 URL `https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=...`
- 加文字 `账号主页：https://...`

**校验条件**：
1. URL path 必须形如 `/user/profile/<24位hex>`
2. URL query 里必须有 `xsec_token` 参数

### 优先级
- 如果用户同时给了 redId 和 URL，优先用 URL（避免不必要的 Edge 调用）
- 如果只给了 redId 且 Edge 已就绪，用 red-id 模式
- 如果只给了 redId 但 Edge 没起，问用户：(a) 帮我启动 Edge 然后用 red-id；或 (b) 我手动复制 URL

### 日期解析
- 标准 `YYYY-MM-DD`
- 中文 `2026年7月22日` → `2026-07-22`
- 相对 `昨天`、`前天` —— 用 `date` 命令算出北京时间

时区固定为北京时间（UTC+8），按笔记 `data.note.time` 毫秒时间戳 +8 小时偏移后判断所属日期。

## 风控规则（最高优先级）

完整规则见 `references/risk_control.md`，核心要点：

1. **小红书 MCP API ≥1.5 秒间隔**：脚本内已用 `time.sleep(1.5)` 实现
2. **命中笔记之间 ≥30 秒间隔**：脚本内已实现
3. **风控信号立即停止**：脚本检测到 "风控/异常/blocked/forbidden/请稍后再试/verify/登录已过期" 关键词立即抛 RuntimeError 退出
4. **飞书 API ≥1.5 秒间隔**：sync_to_lark.py 内已实现
5. **评论采集节流**：`load_all_comments=false`（只取首页 10 条，不主动展开）
6. **CDP 搜索节流**：refresh_token.py 暖机后强制 sleep 10s 再搜索

**禁止修改脚本里的节流参数**（除非用户明确要求放宽）。

## 已知限制

| 限制 | 说明 |
|---|---|
| **red-id 模式需要 Edge** | Edge 必须用 `--remote-debugging-port=9222` 启动，且登录了小土豆炒股账号 |
| **xsec_token 有时效** | 模式 A 的 URL 通常几小时到几天失效；模式 B 每次都拿新的，无此问题 |
| **同账号多端互踢** | xiaohongshu-mcp 和 Edge 网页端如果同时登录同一账号，会互相挤下线（用小号规避） |
| **按天过滤慢** | feeds 列表无时间字段，必须逐篇拉详情（每篇间隔 1.5s，命中笔记 30s） |
| 商品字段不采集 | 小红书网页版不展示带货内容，工具也不解析。商品表保留但保持空。但笔记表里新增「推测带货品类」字段，基于内容关键词做可解释推测，推测不出来填「未识别」 |
| 评论上限 | 每笔记最多 10 条一级评论（首页），不展开二级回复 |

## 资源索引

### scripts/
- `collect.py` —— 主采集脚本 v2.1（支持 `--profile-url` 或 `--red-id` 两种输入）
- `refresh_token.py` —— redId → xsec_token 自动刷新脚本（通过 CDP+Edge）
- `ensure_edge.ps1` —— 启动带 CDP 调试端口的 Edge 实例（含登录态持久化）
- `sync_to_lark.py` —— 飞书同步脚本 v2（输入 JSON，去重 upsert 到多维表格）
- `scheduler.py` —— 每日定时任务 v2.1（支持 token 自动刷新 + 回写飞书）

### references/
- `xhs_mcp_reference.md` —— xiaohongshu-mcp 工具完整文档（启动/登录/MCP 协议/13 工具/响应结构）
- `lark_schema.md` —— 飞书多维表格 4 张表 schema（字段名/ID/类型/关联）
- `risk_control.md` —— 风控规则总览（小红书侧 + 飞书侧）

### assets/
（无）

## 示例对话

**示例 1：red-id 模式（推荐）**

**用户**：帮我采集小红书号 2228145708 在 2026-07-26 的笔记

**助手**：好的，开始采集 redId=2228145708 在 2026-07-26（北京时间）的笔记。
1. 检查 xiaohongshu-mcp + Edge CDP 服务状态...
2. 通过 CDP+Edge 刷新 token：user_id=64632e46000000001002b7c5, nickname=上岸奋斗, fans=1438
3. 调用 user_profile 拿账号信息和笔记列表（60 篇）...
4. 逐篇拉详情，筛选 2026-07-26 的笔记：匹配 3 篇
5. 下载图片（每篇 1 张 webp）
6. 同步到飞书 Wiki...

✅ 完成。账号「上岸奋斗」采集 3 篇笔记、3 张图、3 条评论。

---

**示例 2：profile-url 模式**

**用户**：帮我采集这个账号 2026-07-26 的笔记 https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=YBPe7A1YMYgnqkfmmffAmoAtzBFc5cSFIcVNi_DKzdOd8%3D&xsec_source=pc_feed

**助手**：好的，开始采集账号（user_id=64632e46000000001002b7c5）在 2026-07-26（北京时间）的笔记。
（后续流程同示例 1）

---

**示例 3：red-id 模式但 Edge 没启动**

**用户**：帮我采集小红书号 2228145708 在 2026-07-26 的笔记

**助手**：检测到你想用 redId 模式，但 Edge 调试端口（9222）还没启动。有两种方案：

方案 A（推荐）：我帮你启动 Edge 调试端口，需要你在弹出的 Edge 窗口里扫码登录小土豆炒股（首次一次，之后自动加载 cookies）：
```
powershell -ExecutionPolicy Bypass -File "<skill>/scripts/ensure_edge.ps1"
```

方案 B：你手动在小红书网页端打开该账号主页，复制地址栏完整 URL（含 xsec_token）给我。

你选哪个？
