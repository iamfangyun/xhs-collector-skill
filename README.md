# xhs-collector-skill

小红书账号笔记+评论采集 WorkBuddy Skill。

## 功能

输入 **redId**（小红书号，如 `95466594071`）和**指定日期**（北京时间 `YYYY-MM-DD`），自动：

1. 调用本地 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 采集账号主页信息（粉丝/关注/获赞）
2. 按日期筛选该账号当天发布的所有笔记
3. 采集每篇笔记详情：标题、正文、图片、视频、互动数据、IP、标签
4. 采集每篇笔记评论（最多 20 条一级评论）
5. 下载所有图片到本地缓存
6. 同步到飞书 Wiki 多维表格（4 张表：账号 / 笔记 / 评论 / 商品）
7. **去重规则**：按 `redId` / `笔记ID` / `评论ID` 自动 upsert，存在则更新，不存在则新增

## 前置依赖

### 1. xiaohongshu-mcp（本地服务）

```bash
# 下载 Windows 二进制
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

```bash
# Step 1: 采集
python scripts/collect.py \
  --red-id 95466594071 \
  --date 2026-07-22 \
  --out ./xhs_output

# Step 2: 同步飞书
python scripts/sync_to_lark.py \
  --input ./xhs_output/95466594071_2026-07-22/95466594071_2026-07-22.json \
  --data-root ./xhs_output
```

在 WorkBuddy 里直接用自然语言触发：
> 帮我采集小红书号 95466594071 在 2026-07-22 的笔记

## 风控规则（重要）

严格遵守双重风控：

| 维度 | 规则 |
|---|---|
| 小红书 API | 任意两次调用间隔 ≥1 秒 |
| 笔记间 | ≥30 秒 |
| 评论采集 | `scroll_speed=slow`，每笔记最多 20 条一级评论，不展开二级回复 |
| 风控信号 | 响应出现"风控/异常/blocked/forbidden"立即停止 |
| 飞书 API | 任意两次调用间隔 ≥1 秒 |

详见 [`references/risk_control.md`](references/risk_control.md)。

## 目录结构

```
xhs-collector-skill/
├── SKILL.md                       # Skill 主入口（WorkBuddy 读取）
├── README.md                      # 本文件
├── AUTOMATION.md                  # 定时任务配置文档
├── scripts/
│   ├── collect.py                 # 主采集脚本
│   ├── sync_to_lark.py            # 飞书同步脚本
│   └── scheduler.py               # 每日定时调度脚本（automation 用）
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
- 自动扫描飞书账号表所有 redId
- 账号间隔随机 5~8 分钟
- 遇错即停（失败原因写到飞书「采集日志」表）
- 飞书日志表记录每次任务执行情况

## 已知限制

- **商品字段不采集**：小红书网页版不展示带货内容，平台限制
- **redId 兼容性**：xiaohongshu-mcp 的 `user_profile` 接受 user_id，部分版本 redId 可兼容，否则需先获取 user_id
- **登录态独占**：同账号不能网页多端登录，登录 mcp 后浏览器网页端会被踢
- **评论上限**：每笔记最多 20 条一级评论，不展开二级回复

## License

MIT
