---
name: xhs-collector
description: 小红书账号笔记+评论采集工具。当用户提供一个 redId（小红书号，8-11 位数字）和指定日期（YYYY-MM-DD，北京时间），自动采集该账号当天发布的所有笔记、评论和图片，并同步到飞书 Wiki「小红书采集数据」多维表格。触发场景：用户说"采集小红书账号 XXX 的 YYYY-MM-DD 笔记"、"抓取某账号某天的笔记"、"按日期采集小红书内容"、"同步小红书数据到飞书"等。已存在记录则按 redId/笔记ID/评论ID 去重更新，不重复新增。
agent_created: true
---

# xhs-collector · 小红书采集 Skill

## Overview

输入一个 **redId**（小红书号，如 `95466594071`）和**指定日期**（北京时间 `YYYY-MM-DD`），自动完成：
1. 调用本地 xiaohongshu-mcp 采集该账号主页信息（粉丝/关注/获赞）
2. 按日期筛选该账号当天发布的所有笔记
3. 采集每篇笔记的详情（标题/正文/图片/视频/互动数据/IP/标签）
4. 采集每篇笔记的评论（最多 20 条一级评论）
5. 下载所有图片到本地缓存目录
6. 同步到飞书 Wiki「小红书采集数据」多维表格（4 张表：账号/笔记/评论/商品）
7. **去重规则**：按 redId / 笔记ID / 评论ID 查现有记录，存在则更新，不存在则新增

商品字段不主动采集（小红书网页版不展示带货内容，平台限制）。

## 前置依赖

执行前必须确保以下环境就绪：

### 1. xiaohongshu-mcp 服务运行中
```bash
# 主服务
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
./xiaohongshu-mcp-windows-amd64.exe -port ":18060"
```
监听 `http://localhost:18060/mcp`，详细启动和登录流程见 `references/xhs_mcp_reference.md`。

### 2. cookies.json 已登录
首次或 cookie 过期时，单独运行 `xiaohongshu-login-windows-amd64.exe` 扫码登录。当前登录账号：小土豆炒股（redId: 49274070882）。

### 3. 飞书连接器已启用
WorkBuddy 已连接飞书（用户身份 ou_83159d86de092382962bd7fc86665c82），lark-cli 可直接调用。

## 执行流程

收到用户的采集请求（redId + date）后，按以下步骤执行：

### Step 1: 检查环境
- 用 curl 测试 `http://localhost:18060/mcp` 是否可达
- 调用 `check_login_status` 确认已登录
- 如果服务未启动或未登录，提示用户启动 exe 并扫码

### Step 2: 运行采集脚本
```bash
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\collect.py" \
  --red-id <用户提供的redId> \
  --date <用户提供的YYYY-MM-DD> \
  --out ./xhs_output
```

输出：
- `xhs_output/<redId>_<date>/<redId>_<date>.json` —— 完整采集结果
- `xhs_output/<redId>_<date>/note_<noteId>/image_*.webp` —— 每篇笔记的图片

### Step 3: 同步到飞书
```bash
cd "C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09"
python "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\sync_to_lark.py" \
  --input ./xhs_output/<redId>_<date>/<redId>_<date>.json \
  --data-root ./xhs_output
```

同步完成后给出飞书 URL：https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb

### Step 4: 报告结果
告诉用户：
- 采集到几篇笔记、几张图、几条评论
- 飞书表格里新增/更新了多少条记录
- 任何风控信号或异常

## 输入解析

### redId 识别
用户可能提供的格式：
- 纯数字 `95466594071`
- URL `https://www.xiaohongshu.com/user/profile/95466594071`
- 加文字 "小红书号 95466594071"

提取其中的 8-11 位连续数字作为 redId 传给脚本。

### 日期解析
- 标准 `YYYY-MM-DD`
- 中文 `2026年7月22日` → `2026-07-22`
- 相对 `昨天`、`前天` —— 用 `date` 命令算出北京时间

时区固定为北京时间（UTC+8），按笔记 `time` 字段的毫秒时间戳 +8 小时偏移后判断所属日期。

## 风控规则（最高优先级）

完整规则见 `references/risk_control.md`，核心要点：

1. **小红书 API ≥1 秒间隔**：脚本内已用 `time.sleep(1.0)` 实现
2. **笔记间 ≥30 秒间隔**：脚本内已用 `time.sleep(30.0)` 实现
3. **风控信号立即停止**：脚本检测到 "风控/异常/blocked/forbidden/请稍后再试" 关键词立即抛 RuntimeError 退出
4. **飞书 API ≥1 秒间隔**：sync_to_lark.py 内已实现
5. **评论采集节流**：`scroll_speed=slow`, `limit=20`, `click_more_replies=false`

**禁止修改脚本里的节流参数**（除非用户明确要求放宽）。

## 已知限制

| 限制 | 说明 |
|---|---|
| redId vs user_id | xiaohongshu-mcp 的 `user_profile` 接受 user_id。redId 在某些版本可兼容，不行时需要先用其他方式获取 user_id |
| 商品字段不采集 | 小红书网页版不展示带货内容，工具也不解析。商品表保留但保持空 |
| 评论上限 | 每笔记最多采集 20 条一级评论，不展开二级回复 |
| 登录态独占 | 同账号不能网页多端登录，登录 MCP 后浏览器网页端会被踢 |

## 资源索引

### scripts/
- `collect.py` —— 主采集脚本（输入 redId + date，输出 JSON + 图片）
- `sync_to_lark.py` —— 飞书同步脚本（输入 JSON，去重 upsert 到多维表格）

### references/
- `xhs_mcp_reference.md` —— xiaohongshu-mcp 工具完整文档（启动/登录/MCP 协议/13 工具/响应结构）
- `lark_schema.md` —— 飞书多维表格 4 张表 schema（字段名/ID/类型/关联）
- `risk_control.md` —— 风控规则总览（小红书侧 + 飞书侧）

### assets/
（无）

## 示例对话

**用户**：帮我采集小红书号 95466594071 在 2026-07-22 的笔记

**助手**：好的，开始采集账号 95466594071 在 2026-07-22（北京时间）的笔记。
1. 检查 xiaohongshu-mcp 服务和登录状态...
2. 调用 user_profile 拿账号信息和笔记列表...
3. 筛选 2026-07-22 的笔记：匹配 1 篇
4. 采集笔记详情 + 评论 + 图片（按 30s 间隔节流）...
5. 同步到飞书 Wiki...

✅ 完成。账号「满分💯课代表」采集 1 篇笔记、6 张图、0 条评论。
飞书表格已更新：账号记录已存在（按 redId 去重），笔记记录已新增。
URL: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb
