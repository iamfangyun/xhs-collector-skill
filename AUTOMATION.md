# 定时任务配置（Automation）

本仓库的 `scripts/scheduler.py` 是为 WorkBuddy Automation 设计的定时调度脚本（v3）。

## v3 架构

**弃用 xiaohongshu-mcp，全部通过 Playwright CDP 接管真实 Edge 完成。**

- 采集引擎：collect_v3.py（SSR DOM 提取）
- 同步：sync_to_lark.py（record-id 去重 + lark_run.js 中文 JSON 包装）
- 预检：三重登录检查（cookie + 页面特征 + user/me guest:false）
- 中途复检：每篇 quick_session_check + 每 5 篇 periodic_login_probe
- 熔断：连续 3 次错误立即 sys.exit(3)

## 触发规则（当前配置）

| 项目 | 配置 |
|---|---|
| 触发频率 | **每周二/四/六** |
| 触发时间 | 凌晨 **5:00**（北京时间） |
| 启动抖动 | 随机 **1~30 分钟**（降低时间规律性） |
| 目标日期 | 北京时间**昨天** |
| 账号间隔 | 随机 **30~90 分钟**（多账号时，单账号不触发） |
| 失败策略 | **遇错即停**（任一账号失败立即终止） |
| 熔断 | 连续 3 次错误 / 登录态失效 → exit 3 |

**RRULE**：
```
DTSTART:20260730T050000
RRULE:FREQ=WEEKLY;BYDAY=TU,TH,SA;BYHOUR=5;BYMINUTE=0
```

## 前置条件

### 1. Edge 调试端口已启动 + 登录小红书
```powershell
powershell -ExecutionPolicy Bypass -File "scripts/ensure_edge.ps1"
```

验证：
```bash
curl http://127.0.0.1:9222/json/version
```

如果 Edge 没启动或未登录，scheduler 预检会直接退出（exit 1），**不会盲目请求**（防异常状态被风控标记）。

### 2. 飞书账号表已配置
飞书「账号」表里每个账号的 redId 字段必须填。v3 通过 redId 自动搜索刷新 token，不需要手动维护 URL。

### 3. 飞书连接器已启用
WorkBuddy 已连接飞书，lark-cli 可直接调用。

## 执行流程

```
5:00 automation 触发（周二/四/六）
  ↓
[scheduler.py 启动]
  ↓
随机 sleep 1~30 分钟
  ↓
三重登录预检：
  ① web_session cookie 存在
  ② explore 页无登录按钮
  ③ user/me API guest:false
  ↓ (任一失败 → exit 1，不采集)
计算目标日期 = 北京时间昨天
  ↓
扫描飞书「账号」表全部记录
  ↓
飞书「采集日志」表插入一条 running 记录
  ↓
┌─ 循环每个账号 ─────────────────────────────┐
│  (第 2 个起) 随机 sleep 30~90 分钟          │
│  ↓                                         │
│  调 collect_v3.py：                        │
│    - 三重登录检查 + session 基线           │
│    - redId 搜索刷新 token                  │
│    - SSR DOM 提取 profile + 笔记列表       │
│    - 逐篇拉详情过滤日期                    │
│      * 每篇前 quick_session_check          │
│      * 每 5 篇 periodic_login_probe        │
│      * 连续 3 次错误 → exit 3 熔断         │
│  ↓                                         │
│  调 sync_to_lark.py 同步飞书               │
│  ↓                                         │
│  如果失败 → 立即 break，写日志             │
└────────────────────────────────────────────┘
  ↓
更新日志表记录为 success / partial / failed
  ↓
任务结束，automation 推送结果给用户
```

## 风控参数（脚本内固化）

```python
PACING_API_SEC = 1.5                  # 飞书 API ≥1.5s 间隔
STARTUP_JITTER_MIN = 1                # 启动随机等待最小分钟
STARTUP_JITTER_MAX = 30               # 启动随机等待最大分钟（原 10）
ACCOUNT_GAP_MIN_MIN = 30              # 账号间隔最小分钟（原 5）
ACCOUNT_GAP_MAX_MIN = 90              # 账号间隔最大分钟（原 8）
CONSECUTIVE_ERROR_LIMIT = 3           # 连续错误熔断阈值
PROBE_EVERY_N = 5                     # 每 N 篇做一次 user/me 探测
```

小红书采集层（collect_v3.py）：
- API 间隔：log-normal 分布（中位数 ~2.5s，钳制 [1.5, 8.0]）
- 命中笔记：30-60s 随机
- 每 5 篇小休：60-120s 随机
- 风控信号（结构化 code/msg 检查）→ 立即 sys.exit(3)

## Exit Code 含义

| Code | 含义 | 用户操作 |
|---|---|---|
| 0 | 成功 | 无 |
| 1 | 一般失败（Edge 没起、未登录、采集异常） | 看日志，重启 Edge + 扫码登录 |
| 3 | 登录态失效/熔断（中途复检或连续错误触发） | 重新扫码登录，等 24-48h 再试 |

## 如何添加新账号

直接在飞书「账号」表里**新增一条记录**，填 redId 字段即可。v3 会自动搜索 redId 刷新 token，不需要手动维护 URL。下次 automation 触发时自动发现并采集。

## 如何暂停 / 启用

在 WorkBuddy 里说：
> "暂停小红书采集任务"
> "启用小红书采集任务"

## 故障排查

### exit 1："无法连接 Edge CDP (9222)"
- Edge 没启动 → `powershell -ExecutionPolicy Bypass -File scripts/ensure_edge.ps1`

### exit 1："Edge 里未登录"
- Edge 启动了但登录态过期
- 在 Edge 窗口重新扫码登录小红书账号

### exit 3："登录态失效/熔断"
- 采集中途 session 失效，或连续 3 次错误
- **不要立即重试**，等 24-48 小时让风控冷却
- 重新扫码登录后下次 automation 自动恢复

### exit 3："风控信号"
- 响应 code/msg 含风控关键词
- 等 24-48 小时再试，或换小号

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
