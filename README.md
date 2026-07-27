# xhs-collector-skill

小红书账号笔记+评论采集 WorkBuddy Skill（**v2.1 - 支持 redId 自动刷新 token**）。

## ⚡ v2.1 重要变化

> **新增 `--red-id` 模式：只需要提供 redId（小红书号），token 会自动通过 CDP+Edge 刷新。**

之前 v2.0 要求每次都手动从浏览器复制带 `xsec_token` 的主页 URL，token 几小时就失效。v2.1 通过 CDP 接管真实 Edge 浏览器，自动搜索 redId 并从拦截到的 onebox API 拿到最新 token，**用户只需扫码登录一次，之后永久自动化**。

`--profile-url` 模式仍然保留，向后兼容。

详见 [v2-关键变化](#v21-关键变化)。

## 功能

支持**两种输入模式**：

| 模式 | 输入 | 何时用 | 前置条件 |
|---|---|---|---|
| **A. red-id**（推荐） | redId（小红书号，纯数字）+ 日期 | 只知道 redId，或想让 token 自动刷新 | Edge 已用调试端口启动 + 登录小土豆炒股 |
| **B. profile-url** | 主页完整 URL（带 `xsec_token`）+ 日期 | 用户已经手动从浏览器复制了 URL | 无 |

两种模式都会自动：

1. 模式 A 自动调用 `refresh_token.py` 通过 CDP+Edge 拿到最新 `user_id` + `xsec_token`；模式 B 直接解析 URL
2. 调用本地 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 采集账号主页信息（粉丝/关注/获赞）
3. 按日期筛选该账号当天发布的所有笔记（逐篇拉详情，因为 feeds 列表无时间字段）
4. 采集每篇笔记��情：标题、正文、图片、视频、互动数据、IP、标签
5. 采集每篇笔记评论（首页最多 10 条一级评论）
6. 下载所有图片到本地缓存
7. 同步到飞书 Wiki 多维表格（4 张表：账号 / 笔记 / 评论 / 商品）
8. **去重规则**：按 `redId` / `笔记ID` / `评论ID` 自动 upsert，存在则更新，不存在则新增

## 前置依赖

### 1. xiaohongshu-mcp v2.0.0+（本地服务）

```bash
# 下载 Windows 二进制（v2.0.0 或更新）
# https://github.com/xpzouying/xiaohongshu-mcp/releases

# 启动主服务
./xiaohongshu-mcp-windows-amd64.exe -port ":18060"

# 首次登录（扫码）
./xiaohongshu-login-windows-amd64.exe
```

服务监听 `http://localhost:18060/mcp`。

### 2. (仅 red-id 模式需要) Edge 浏览器带 CDP 调试端口

```powershell
# 推荐：用 skill 自带的启动脚本
powershell -ExecutionPolicy Bypass -File scripts/ensure_edge.ps1
```

或手动启动：
```bash
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" \
  --remote-debugging-port=9222 \
  --user-data-dir="$LOCALAPPDATA\Temp\edge_crawler_data" \
  --no-first-run --no-default-browser-check \
  https://www.xiaohongshu.com/explore
```

首次启动后在弹出的 Edge 窗口扫码登录小土豆炒股账号，cookies 会持久化到 `--user-data-dir`，之后每次启动自动加载登录状态。

**为什么不能用 Chrome 或 Playwright Chromium**：
- Chrome 是用户日常浏览器，不能干扰
- Playwright Chromium 会被小红书指纹识别为自动化工具，触发登录墙
- CDP 接管真实 Edge = 真实指纹 + 真实登录态，不被检测

### 3. 飞书连接器

WorkBuddy 已连接飞书，`lark-cli` 可直接调用。需先在飞书 Wiki 创建多维表格 Base 并写入对应的 schema（4 张表 + 字段），详见 [`references/lark_schema.md`](references/lark_schema.md)。

### 4. 飞书多维表格配置

如要复用此 skill，需修改 `scripts/sync_to_lark.py` 里的常量：

```python
LARK = r"<你的 lark-cli 路径>"
BASE_TOKEN = "<你的飞书 Base token>"
TBL_ACCOUNT = "<你的账号表 ID>"
TBL_NOTE = "<你的笔记表 ID>"
TBL_COMMENT = "<你的评论表 ID>"
```

## 用法

### 单次采集（red-id 模式，推荐）

```bash
# Step 1: 确保 Edge CDP 已启动 + 已登录
powershell -ExecutionPolicy Bypass -File scripts/ensure_edge.ps1

# Step 2: 采集（只需 redId + 日期）
python scripts/collect.py --red-id 2228145708 --date 2026-07-26 --out ./xhs_output

# Step 3: 同步飞书
python scripts/sync_to_lark.py \
  --input ./xhs_output/<redId>_<nickname>/<redId>_<nickname>_2026-07-26.json \
  --data-root ./xhs_output
```

### 单次采集（profile-url 模式）

```bash
# Step 1: 采集
python scripts/collect.py \
  --profile-url "https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=XXXX&xsec_source=pc_feed" \
  --date 2026-07-26 \
  --out ./xhs_output

# Step 2: 同步飞书
python scripts/sync_to_lark.py \
  --input ./xhs_output/<redId>_<nickname>/<redId>_<nickname>_2026-07-26.json \
  --data-root ./xhs_output
```

### 如何获取主页 URL（profile-url 模式）

1. 在小红书网页端（`www.xiaohongshu.com`）打开目标账号的主页
2. 复制浏览器地址栏的完整 URL（形如下方），整段提供给 skill：
   ```
   https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=YBPe7A1YMYgnqkfmmffAmoAtzBFc5cSFIcVNi_DKzdOd8%3D&xsec_source=pc_feed
   ```

**注意**：`xsec_token` 有时效性（通常几小时到几天）。如果想避免手动刷新，请用 red-id 模式。

在 WorkBuddy 里直接用自然语言触发：
> 帮我采集小红书号 2228145708 在 2026-07-26 的笔记

## v2.1 关键变化

| 维度 | v2.0 | v2.1（当前） |
|---|---|---|
| 输入参数 | 只有 `--profile-url` | 新增 `--red-id`（二选一） |
| token 获取 | 用户手动从浏览器复制 | `--red-id` 模式自动通过 CDP+Edge 刷新 |
| 浏览器依赖 | 无 | `--red-id` 模式需要 Edge 带 9222 调试端口 |
| 定时任务 | token 失效就失败 | 自动切到 `--red-id` 模式重试，并把新 URL 回写到飞书 |
| 用户体验 | 每天可能要手动复制 URL | 扫码登录一次，之后永久自动化 |

## v2.0 关键变化（v1 → v2.0）

| 维度 | v1 (已废弃) | v2.0 | v2.1（当前） |
|---|---|---|---|
| 输入参数 | `--red-id 95466594071` | `--profile-url "https://...?xsec_token=..."` | 二选一：`--red-id` 或 `--profile-url` |
| user_profile 调用 | 直接传 redId（部分版本兼容） | 解析 URL 得到 user_id + xsec_token | 同 v2.0（red-id 模式先自动拿 token） |
| user_profile 返回 | `basic_info` / `notes` | `userBasicInfo` / `interactions` / `feeds` | 同 v2.0 |
| get_feed_detail 参数 | `note_id` | `feed_id` | 同 v2.0 |
| 按天过滤 | feeds 列表里的 time 字段 | 必须逐篇拉 detail（feeds 里无时间） | 同 v2.0 |
| 评论采集 | `load_all_comments=true, limit=20` | `load_all_comments=false`（首页 10 条） | 同 v2.0 |
| HTTP 头 | 默认 | 必须带 `Accept: application/json, text/event-stream` | 同 v2.0 |
| 飞书账号表"主页链接"字段 | markdown 格式 `[昵称](URL)` | 纯 URL（含 xsec_token） | 同 v2.0，token 失效时自动更新 |
| API 间隔 | 1.0 秒 | 1.5 秒 | 同 v2.0 |

## 风控规则（重要）

严格遵守双重风控：

| 维度 | 规则 |
|---|---|
| 小红书 API | 任意两次调用间隔 ≥1.5 秒 |
| 命中笔记间 | ≥30 秒 |
| 批量小休 | 每拉 10 篇详情休 15 秒 |
| 评论采集 | `load_all_comments=false`，只取首页 10 条，不展开二级回复 |
| 风控信号 | 响应出现"风控/异常/blocked/forbidden/verify/登录已过期"立即停止 |
| 飞书 API | 任意两次调用间隔 ≥1.5 秒 |
| CDP 搜索节流 | refresh_token.py 暖机后强制 sleep 10s 再搜索 |

详见 [`references/risk_control.md`](references/risk_control.md)。

## 目录结构

```
xhs-collector-skill/
├── SKILL.md                       # Skill 主入口（WorkBuddy 读取）
├── README.md                      # 本文件
├── AUTOMATION.md                  # 定时任务配置文档
├── scripts/
│   ├── collect.py                 # 主采集脚本 v2.1（支持 --red-id 或 --profile-url）
│   ├── refresh_token.py           # redId → xsec_token 自动刷新（CDP+Edge）
│   ├── ensure_edge.ps1            # 启动带 CDP 调试端口的 Edge（含登录态持久化）
│   ├── sync_to_lark.py            # 飞书同步脚本 v2
│   └── scheduler.py               # 每日定时调度脚本 v2.1（支持 token 自动刷新）
└── references/
    ├── xhs_mcp_reference.md       # xiaohongshu-mcp 完整工具文档
    ├── lark_schema.md             # 飞书多维表格 schema
    └── risk_control.md            # 风控规则总览
```

## 定时任务（Automation）

支持每天凌晨自动采集昨天所有账号的笔记，配置见 [`AUTOMATION.md`](AUTOMATION.md)。

核心特性：
- 每天凌晨 5:00 触发
- 启动后随机 sleep 1~10 分钟（模拟人类作息）
- 自动扫描飞书账号表所有记录
- profile_url 含 xsec_token → 直接采集
- profile_url 不含 token 或 token 失效 → 自动切到 `--red-id` 模式刷新
- 刷新成功后自动回写新 URL 到飞书账号表（下次直接用）
- 账号间隔随机 5~8 分钟
- 遇错即停（失败原因写到飞书「采集日志」表）

## 已知限制

- **red-id 模式需要 Edge**：Edge 必须用 `--remote-debugging-port=9222` 启动，且登录了小土豆炒股账号
- **xsec_token 有时效**：模式 A 的 URL 通常几小时到几天失效；模式 B 每次都拿新的，无此问题
- **同账号多端互踢**：xiaohongshu-mcp 和 Edge 网页端如果同时登录同一账号，会互相挤下线（用小号规避）
- **按天过滤慢**：feeds 列表无时间字段，必须逐篇拉详情（每篇间隔 1.5s，命中笔记 30s）
- **商品字段不采集**：小红书网页版不展示带货内容，平台限制
- **评论上限**：每笔记最多 10 条一级评论（首页），不展开二级回复

## License

MIT
