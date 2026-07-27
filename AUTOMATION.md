# 定时任务配置（Automation）

本仓库的 `scripts/scheduler.py` 是为 WorkBuddy Automation 设计的每日调度脚本。

## v2.1 新特性：token 自动刷新

v2.1 scheduler 在 v2.0 基础上新增了 token 自动刷新能力：

| 账号 profile_url 状态 | v2.0 行为 | v2.1 行为 |
|---|---|---|
| URL 含 xsec_token | 直接采集 | 直接采集（同 v2.0） |
| URL 不含 xsec_token | 跳过 + 报警 | 自动切 `--red-id` 模式刷新 token 后采集 |
| 采集失败（token 过期） | 失败 + 停止 | 自动 fallback 到 `--red-id` 模式重试 |
| 刷新成功 | / | 把新 URL 回写到飞书账号表「主页链接」字段 |

**好处**：用户不再需要每天手动更新飞书账号表里的 URL，只要 Edge 已经启动并登录过一次，永久自动化。

## 触发规则

| 项目 | 配置 |
|---|---|
| 触发时间 | 每天凌晨 **5:00**（北京时间） |
| 启动延迟 | 随机 **1~10 分钟**（模拟人类作息不规律） |
| 目标日期 | 北京时间**昨天**（避免凌晨跨日问题） |
| 账号间隔 | 随机 **5~8 分钟** |
| 失败策略 | **遇错即停**（任一账号失败立即终止后续） |
| 服务保证 | xiaohongshu-mcp 必须 24h 运行；如要用 red-id 刷新模式，Edge 也需保持运行 |

## 前置条件

### 1. xiaohongshu-mcp 服务运行中
```bash
./xiaohongshu-mcp-windows-amd64.exe -port ":18060"
```

### 2. (推荐) Edge 调试端口已启动 + 登录小土豆炒股
```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts\ensure_edge.ps1"
```

启动后 curl 验证：
```bash
curl http://127.0.0.1:9222/json/version
```

如果没启动，scheduler 会自动降级到「只采集 profile_url 里已有 token 的账号」，含 token 过期的账号会失败。

### 3. 飞书账号表已配置
飞书「账号」表里每个账号的 redId 字段必须填。profile_url（主页链接）字段：
- 如果填了带 xsec_token 的完整 URL → 直接用
- 如果空着或 URL 不含 token → 自动用 red-id 模式刷新

## 执行流程

```
5:00 automation 触发
  ↓
[scheduler.py 启动]
  ↓
随机 sleep 1~10 分钟
  ↓
计算目标日期 = 北京时间昨天
  ↓
扫描飞书「账号」表全部记录
  ├─ URL 含 xsec_token: 标记 mode=profile-url
  └─ URL 不含 token:    标记 mode=red-id (needs_refresh=true)
  ↓
飞书「采集日志」表插入一条 running 记录
  ↓
┌─ 循环每个账号 ─────────────────────────┐
│  (第 2 个起) 随机 sleep 5~8 分钟        │
│  ↓                                     │
│  [尝试] mode=profile-url (如果有 token) │
│    ├─ 成功: 继续                        │
│    └─ 失败且是 token 问题:              │
│        [fallback] mode=red-id 重试      │
│          ├─ 成功: 回写新 URL 到飞书      │
│          └─ 失败: break                 │
│  ↓                                     │
│  调用 sync_to_lark.py 同步飞书          │
│  ↓                                     │
│  如果失败 → 立即 break，写日志          │
└────────────────────────────────────────┘
  ↓
更新日志表记录为 success / partial / failed
  ↓
任务结束，automation 推送结果给用户
```

## 风控规则（脚本内固化）

```python
PACING_API_SEC = 1.5                # 飞书/小红书 API ≥1.5s 间隔
STARTUP_JITTER_MIN = 1             # 启动随机等待最小分钟
STARTUP_JITTER_MAX = 10            # 启动随机等待最大分钟
ACCOUNT_GAP_MIN_MIN = 5            # 账号间隔最小分钟
ACCOUNT_GAP_MAX_MIN = 8            # 账号间隔最大分钟
```

小红书 API 层：
- 任意两次 MCP `tools/call` ≥1.5s（collect.py 内 `time.sleep(1.5)`）
- 笔记间 ≥30s（collect.py 内 `time.sleep(30.0)`）
- `load_all_comments=false`，只取首页 10 条
- 风控关键词触发立即抛 RuntimeError：`风控/异常/blocked/forbidden/请稍后再试/verify/登录已过期`

CDP 搜索层（red-id 模式）：
- refresh_token.py 暖机 5s + 反风控间隔 10s + 搜索等待 12s
- 全程用 Edge 真实指纹，不被识别为自动化

## 如何配置 Automation（在 WorkBuddy 里）

### 方法 1：通过对话

告诉 WorkBuddy：
> "创建一个定时任务，每天凌晨 5 点运行小红书每日采集"

WorkBuddy 会用 `automation_update` 工具创建。

### 方法 2：RRULE 配置

```
DTSTART:20260727T050000
RRULE:FREQ=DAILY;BYHOUR=5;BYMINUTE=0
```

完整 automation 配置示例：

```json
{
  "name": "小红书每日采集",
  "scheduleType": "recurring",
  "rrule": "DTSTART:20260727T050000\nRRULE:FREQ=DAILY;BYHOUR=5;BYMINUTE=0",
  "cwds": "C:\\Users\\Administrator\\WorkBuddy\\2026-07-24-22-52-09",
  "status": "ACTIVE",
  "prompt": "执行小红书每日采集任务。运行命令：python \"C:\\Users\\Administrator\\.workbuddy\\skills\\xhs-collector\\scripts\\scheduler.py\"。这个脚本会自动扫描飞书账号表、采集昨天所有账号的笔记、同步到飞书多维表格。执行前请确认 xiaohongshu-mcp 服务在 http://localhost:18060/mcp 运行，并建议 Edge 也启动了调试端口 9222（用于 token 自动刷新）。完成后报告采集到的笔记数、评论数、图片数，以及飞书日志表的状态。"
}
```

## 飞书日志表 Schema

每次任务执行会在飞书「采集日志」表里写一条记录：

| 字段 | 类型 | 说明 |
|---|---|---|
| 任务开始时间 | datetime | 任务实际启动时间（含 jitter 之后） |
| 任务结束时间 | datetime | 任务结束时间 |
| 目标日期 | text | YYYY-MM-DD（北京时间昨天） |
| 待采集账号数 | number | 扫描到的账号总数 |
| 成功账号数 | number | 成功采集的账号数 |
| 失败账号数 | number | 失败的账号数 |
| 总笔记数 | number | 所有账号采集到的笔记总数 |
| 总评论数 | number | 评论总数 |
| 总图片数 | number | 图片总数 |
| 任务状态 | select | `success` / `partial` / `failed` / `running` |
| 随机等待秒数 | number | 启动 jitter 实际等待的秒数 |
| 失败账号列表 | text | `redId:nickname - 错误原因` 多行 |
| 错误详情 | text | 完整错误堆栈 |

## 如何添加新账号

直接在飞书「账号」表里**新增一条记录**，填 redId 字段即可。
- 如果「主页链接」字段填了带 xsec_token 的完整 URL → 直接用
- 如果「主页链接」字段留空或不带 token → 自动用 red-id 模式刷新（前提是 Edge 已启动）

下次 automation 触发时自动发现并采集。不需要修改任何代码。

## 如何暂停 / 启用

在 WorkBuddy 里说：
> "暂停小红书每日采集任务"

或者：
> "启用小红书每日采集任务"

也可以在 WorkBuddy 设置界面手动切换 ACTIVE / PAUSED。

## 如何手动触发测试

如果想立即跑一次（不等凌晨 5 点），在 WorkBuddy 里说：
> "手动跑一次小红书每日采集"

或者直接命令行执行：
```bash
cd <工作目录>
python "<skill路径>/scripts/scheduler.py"
```

## 故障排查

### 报错"未登录"或"MCP 服务未启动"
- 启动 `xiaohongshu-mcp-windows-amd64.exe -port ":18060"`
- 必要时重新扫码登录：`xiaohongshu-login-windows-amd64.exe`
- 服务起来后下次 automation 自动恢复

### 报错"无法连接 Edge CDP (9222)"
- 意味着 red-id 自动刷新模式不可用，只能采集 profile_url 里已有 token 的账号
- 启动 Edge：`powershell -ExecutionPolicy Bypass -File scripts/ensure_edge.ps1`
- 首次需要在 Edge 里扫码登录小土豆炒股
- 起来后下次 automation 自动用 red-id 模式刷新所有账号的 token

### 报错"Edge 里 web_session 不存在"
- 意味着 Edge 启动了但登录态过期了（被挤下线、cookies 失效等）
- 在 Edge 窗口里重新扫码登录小土豆炒股
- cookies 会持久化，之后不需要再扫

### 报错"风控信号"
- scheduler 检测到响应里出现风控关键词立即停止
- 飞书日志表里有详细错误
- 等 24~48 小时后再试，或换小号

### 飞书写入失败
- 检查飞书连接器是否还在 ACTIVE 状态
- 检查 BASE_TOKEN 是否还有效（Base 被删/移动会失效）

### 某个账号采集失败
- 飞书日志表「失败账号列表」字段会列出具体哪个账号失败
- 「错误详情」字段有完整堆栈
- 由于「遇错即停」策略，后续账号不会被执行，需要等第二天或手动触发
- 如果只是 token 过期，v2.1 会自动 fallback 重试，不需要人工干预
