# xhs-collector-skill

小红书账号笔记+评论采集 WorkBuddy Skill（**v2 - 适配 xiaohongshu-mcp v2.0.0+**）。

## ⚡ v2 重要变化

> **输入参数从 `redId` 改为 `profile_url`（完整主页 URL，必须带 `xsec_token`）**

原因：xiaohongshu-mcp v2.0.0 的 `user_profile` 工具强制要求 `user_id`（24 位 hex）+ `xsec_token` 两个参数，而 `redId`（8-11 位数字）既不是 `user_id` 也无法换取 `xsec_token`，所以只提供 redId 无法采集。

详见 [v2-schema-说明](#v2-关键变化)。

## 功能

输入**小红书账号主页完整 URL**（必须带 `xsec_token` 参数）和**指定日期**（北京时间 `YYYY-MM-DD`），自动：

1. 从 URL 解析出 `user_id` + `xsec_token`
2. 调用本地 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 采集账号主页信息（粉丝/关注/获赞）
3. 按日期筛选该账号当天发布的所有笔记（逐篇拉详情，因为 feeds 列表无时间字段）
4. 采集每篇笔记详情：标题、正文、图片、视频、互动数据、IP、标签
5. 采集每篇笔记评论（首页最多 10 条一级评论）
6. 下载所有图片到本地缓存
7. 同步到飞书 Wiki 多维表格（4 张表：账号 / 笔记 / 评论 / 商品）
8. **去重规则**：按 `redId` / `笔记ID` / `评论ID` 自动 upsert，存在则更新，不存在则新增

## 前��依赖

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

### 2. 飞书连接器

WorkBuddy 已连接飞书，`lark-cli` 可直接调用。需先在飞书 Wiki 创建多维表格 Base 并写入对应的 schema（4 张表 + 字段），详见 [`references/lark_schema.md`](references/lark_schema.md)。

### 3. 飞书多维表格配置

如要复用此 skill，需修改 `scripts/sync_to_lark.py` 里的常量：

```python
LARK = r"<你的 lark-cli 路径>"
BASE_TOKEN = "<你的飞书 Base token>"
TBL_ACCOUNT = "<你的账号表 ID>"
TBL_NOTE = "<你的笔记表 ID>"
TBL_COMMENT = "<你的评论表 ID>"
```

## 用法

### 单次采集

```bash
# Step 1: 采集（注意 v2 用 --profile-url 而非 --red-id）
python scripts/collect.py \
  --profile-url "https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=XXXX&xsec_source=pc_feed" \
  --date 2026-07-26 \
  --out ./xhs_output

# Step 2: 同步飞书
python scripts/sync_to_lark.py \
  --input ./xhs_output/<redId>_<nickname>/<redId>_<nickname>_2026-07-26.json \
  --data-root ./xhs_output
```

### 如何获取主页 URL

1. 在小红书网页端（`www.xiaohongshu.com`）打开目标账号的主页
2. 复制浏览器地址栏的完整 URL（形如下方），整段提供给 skill：
   ```
   https://www.xiaohongshu.com/user/profile/64632e46000000001002b7c5?xsec_token=YBPe7A1YMYgnqkfmmffAmoAtzBFc5cSFIcVNi_DKzdOd8%3D&xsec_source=pc_feed
   ```

**注意**：`xsec_token` 有时效性（通常几小时到几天），失效后需要重新打开网页端复制最新 URL。

在 WorkBuddy 里直接用自然语言触发：
> 帮我采集 https://www.xiaohongshu.com/user/profile/xxx?xsec_token=yyy 在 2026-07-26 的笔记

## v2 关键变化

| 维度 | v1 (已废弃) | v2 (当前) |
|---|---|---|
| 输入参数 | `--red-id 95466594071` | `--profile-url "https://...?xsec_token=..."` |
| user_profile 调用 | 直接传 redId（部分版本兼容） | 解析 URL 得到 user_id + xsec_token |
| user_profile 返回 | `basic_info` / `notes` | `userBasicInfo` / `interactions` / `feeds` |
| get_feed_detail 参数 | `note_id` | `feed_id` |
| 按天过滤 | feeds 列表里的 time 字段 | 必须逐篇拉 detail（feeds 里无时间） |
| 评论采集 | `load_all_comments=true, limit=20` | `load_all_comments=false`（首页 10 条） |
| HTTP 头 | 默认 | 必须带 `Accept: application/json, text/event-stream` |
| 飞书账号表"主页链接"字段 | markdown 格式 `[昵称](URL)` | 纯 URL（含 xsec_token） |
| API 间隔 | 1.0 秒 | 1.5 秒 |

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

详见 [`references/risk_control.md`](references/risk_control.md)。

## 目录结构

```
xhs-collector-skill/
├── SKILL.md                       # Skill 主入口（WorkBuddy 读取）
├── README.md                      # 本文件
├── AUTOMATION.md                  # 定时任务配置文档
├── scripts/
│   ├── collect.py                 # 主采集脚本 v2（输入 profile-url）
│   ├── sync_to_lark.py            # 飞书同步脚本 v2
│   └── scheduler.py               # 每日定时调度脚本 v2
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
- 自动扫描飞书账号表所有记录的"主页链接"字段（含 xsec_token）
- 链接不含 xsec_token 的账号自动跳过
- 账号间隔随机 5~8 分钟
- 遇错即停（失败原因写到飞书「采集日志」表）
- 飞书日志表记录每次任务执行情况

## 已知限制

- **必须主页 URL**：xiaohongshu-mcp v2.0.0 强制要求 user_id + xsec_token，redId 不行
- **xsec_token 有时效**：通常几小时到几天失效，过期需用户重新打开网页端复制新 URL
- **按天过滤慢**：feeds 列表无时间字段，必须逐篇拉详情（每篇间隔 1.5s，命中笔记 30s）
- **商品字段不采集**：小红书网页版不展示带货内容，平台限制
- **登录态独占**：同账号不能网页多端登录，登录 mcp 后浏览器网页端会被踢
- **评论上限**：每笔记最多 10 条一级评论（首页），不展开二级回复

## License

MIT
