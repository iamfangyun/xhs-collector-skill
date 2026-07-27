---
name: xhs-collector
description: 小红书账号笔记+评论采集工具。当用户提供一个小红��账号主页完整 URL（必须带 xsec_token 参数）和指定日期（YYYY-MM-DD，北京时间），自动采集该账号当天发布的所有笔记、评论和图片，并同步到飞书 Wiki「小红书采集数据」多维表格。触发场景：用户说"采集小红书账号 XXX 的 YYYY-MM-DD 笔记"、"抓取某账号某天的笔记"、"按日期采集小红书内容"、"同步小红书数据到飞书"等。已存在记录则按 redId/笔记ID/评论ID 去重更新，不重复新增。
agent_created: true
---

# xhs-collector · 小红书采集 Skill (v2)

## Overview

输入一个**小红书账号主页完整 URL**（必须带 `xsec_token` 参数）和**指定日期**（北京时间 `YYYY-MM-DD`），自动完成：
1. 从 URL 解析出 `user_id`（24 位 hex）和 `xsec_token`
2. 调用本地 xiaohongshu-mcp 采集该账号主页信息（粉丝/关注/获赞）
3. 按日期筛选该账号当天发布的所有笔记（必须逐篇拉详情才有时间字段）
4. 采集每篇笔记的详情（标题/正文/图片/视频/互动数据/IP/标签）
5. 采集每篇笔记的评论（首页最多 10 条一级评论）
6. 下载所有图片到本地缓存目录
7. 同步到飞书 Wiki「小红书采集数据」多维表格（4 张表：账号/笔记/评论/商品）
8. **去重规则**：按 redId / 笔记ID / 评论ID 查现有记录，存在则更新，不存在则新增

商品字段不主动采集（小红书网页版不展示带货内容，平台限制）。

## ⚠️ 为什么必须用主页 URL 而不是 redId？

xiaohongshu-mcp v2.0.0 的 `user_profile` 工具强制要求两个参数：
- `user_id`：小红书内部 ID，**24 位 hex**（如 `64632e46000000001002b7c5`）
- `xsec_token`：访问令牌，由小红书网页端生成，有时效性

而 `redId`（8-11 位数字，如 `2228145708`）是用户短号，**既不是 user_id 也无法换取 xsec_token**。所以只提供 redId 是采集不了的。

获取主页 URL 的方法：
1. 在小红书网页端（`www.xiaohongshu.com`）打开目标账号的主页
2. 复制浏览器地址栏的完整 URL，形如：
   ```
   https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=YBPe7A1YMYgnqkfmmffAmoAtzBFc5cSFIcVNi_DKzdOd8%3D&xsec_source=pc_feed
   ```
3. 把这个 URL 整段提供给 skill

**注意**：`xsec_token` 有时效性（通常几小时到几天），失效后 collect.py 会报错提示，用户需要重新打开网页端复制最新 URL 更新。

## 前置依赖

### 1. xiaohongshu-mcp 服务运行中
```bash
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
./xiaohongshu-mcp-windows-amd64.exe -port ":18060"
```
监听 `http://localhost:18060/mcp`，详细启动和登录流程见 `references/xhs_mcp_reference.md`。

### 2. cookies.json 已登录
首次或 cookie 过期时，单独运行 `xiaohongshu-login-windows-amd64.exe` 扫码登录。当前登录账号：小土豆炒股（redId: 49274070882）。

### 3. 飞书连接器已启用
WorkBuddy 已连接飞书（用户身份 ou_83159d86de092382962bd7fc86665c82），lark-cli 可直接调用。

## 执行流程

收到用户的采集请求（主页 URL + date）后，按以下步骤执行：

### Step 1: 检查环境
- 用 curl 测试 `http://localhost:18060/mcp` 是否可达
- 如果服务未启动，提示用户启动 exe 并扫码

### Step 2: 运行采集脚本
```bash
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\collect.py" \
  --profile-url "<用户提供的完整主页URL>" \
  --date <用户提供的YYYY-MM-DD> \
  --out ./xhs_output
```

输出：
- `xhs_output/<redId>_<nickname>/<redId>_<nickname>_<date>.json` —— 完整采集结果
- `xhs_output/<redId>_<nickname>/images/<note_id>_<idx>.webp` —— 每篇笔记的图片

### Step 3: 同步到飞书
```bash
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\sync_to_lark.py" \
  --input ./xhs_output/<redId>_<nickname>/<redId>_<nickname>_<date>.json \
  --data-root ./xhs_output
```

同步完成后给出飞书 URL：https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb

### Step 4: 报告结果
告诉用户：
- 采集到几篇笔记、几张图、几条评论
- 飞书表格里新增/更新了多少条记录
- 任何风控信号或异常

## 输入解析

### 主页 URL 识别
用户可能提供的格式：
- 完整 URL `https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=...`
- 加文字 "账号主页：https://www.xiaohongshu.com/user/profile/xxx?xsec_token=..."

**校验条件**：
1. URL path 必须形如 `/user/profile/<24位hex>`
2. URL query 里必须有 `xsec_token` 参数

如果用户只提供 redId（纯数字），要主动询问："需要这个账号主页的完整 URL（含 xsec_token），请在小红书网页端打开该账号主页，复制地址栏完整 URL 给我。"

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

**禁止修改脚本里的节流参数**（除非用户明确要求放宽）。

## 已知限制

| 限制 | 说明 |
|---|---|
| **必须主页 URL** | xiaohongshu-mcp v2.0.0 强制要求 user_id + xsec_token，redId 不行 |
| **xsec_token 有时效** | 通常几小时到几天失效，过期需用户重新打开网页端复制新 URL |
| **按天过滤慢** | feeds 列表无时间字段，必须逐篇拉详情（每篇间隔 1.5s，命中笔记 30s） |
| 商品字段不采集 | 小红书网页版不展示带货内容，工具也不解析。商品表保留但保持空 |
| 评论上限 | 每笔记最多 10 条一级评论（首页），不展开二级回复 |
| 登录态独占 | 同账号不能网页多端登录，登录 MCP 后浏览器网页端会被踢 |

## 资源索引

### scripts/
- `collect.py` —— 主采集脚本 v2（输入主页URL+date，输出 JSON + 图片）
- `sync_to_lark.py` —— 飞书同步脚本 v2（输入 JSON，去重 upsert 到多维表格）
- `scheduler.py` —— 每日定时任务 v2（从飞书账号表读 URL，逐账号采集昨天数据）

### references/
- `xhs_mcp_reference.md` —— xiaohongshu-mcp 工具完整文档（启动/登录/MCP 协议/13 工具/响应结构）
- `lark_schema.md` —— 飞书多维表格 4 张表 schema（字段名/ID/类型/关联）
- `risk_control.md` —— 风控规则总览（小红书侧 + 飞书侧）

### assets/
（无）

## 示例对话

**用户**：帮我采集这个账号 2026-07-26 的笔记 https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=YBPe7A1YMYgnqkfmmffAmoAtzBFc5cSFIcVNi_DKzdOd8%3D&xsec_source=pc_feed

**助手**：好的，开始采集账号（user_id=64632e46000000001002b7c5）在 2026-07-26（北京时间）的笔记。
1. 检查 xiaohongshu-mcp 服务状态...
2. 调用 user_profile 拿账号信息和笔记列表（60 篇）...
3. 逐篇拉详情，筛选 2026-07-26 的笔记：匹配 3 篇
4. 下载图片（每篇 1 张 webp）
5. 同步到飞书 Wiki...

✅ 完成。账号「上岸奋斗」采集 3 篇笔记、3 张图、3 条评论。
飞书表格已更新：账号记录已存在（按 redId 去重），3 条笔记记录已新增（含图片附件）。
URL: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb

---

**用户（错误示例）**：帮我采集小红书号 2228145708 在 2026-07-26 的笔记

**助手**：redId（2228145708）不能直接采集，我需要这个账号主页的完整 URL（含 xsec_token）。
请在小红书网页端（www.xiaohongshu.com）打开该账号主页，复制浏览器地址栏的完整 URL 给我，形如：
`https://www.xiaohongshu.com/user/profile/xxxx?xsec_token=yyyy`
