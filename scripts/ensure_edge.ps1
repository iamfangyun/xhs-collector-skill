# ensure_edge.ps1
# ==============================================================================
# 启动一个带 CDP 调试端口的独立 Edge 实例, 供 refresh_token.py 通过 Playwright 接管。
# 这个实例使用独立的临时 user-data-dir, 不影响用户日常的 Chrome / Edge。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File ensure_edge.ps1
#
# 行为:
#   1. 如果已经有 Edge 进程占用了 9222 端口, 直接退出 (复用)
#   2. 否则启动新的 Edge:
#      - --remote-debugging-port=9222
#      - --user-data-dir=<持久化目录>  (cookies / localStorage 跨重启保留)
#      - --no-first-run --no-default-browser-check (避免首次启动弹窗)
#      - 自动打开 https://www.xiaohongshu.com/explore
#
# 登录状态持久化:
#   - user-data-dir 默认在: C:\Users\<USER>\AppData\Local\Temp\edge_crawler_data
#   - 第一次启动后, 在弹出的 Edge 窗口里扫码登录小土豆炒股账号
#   - 之后每次启动都会自动加载 cookies, 不需要再扫码
#   - 如果某天 cookies 过期被踢下线, 重新扫一次码即可
#
# 检查端口是否监听:
#   curl http://127.0.0.1:9222/json/version
# ==============================================================================

param(
    [int]$Port = 9222,
    [string]$UserDataDir = "$env:LOCALAPPDATA\Temp\edge_crawler_data",
    [string]$StartUrl = "https://www.xiaohongshu.com/explore"
)

$ErrorActionPreference = "Stop"

# Edge 二进制路径 (常见两个位置都查一下)
$EdgeCandidates = @(
    "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
)
$EdgePath = $null
foreach ($p in $EdgeCandidates) {
    if (Test-Path $p) {
        $EdgePath = $p
        break
    }
}
if (-not $EdgePath) {
    Write-Host "[ERROR] 找不到 msedge.exe, 请确认 Edge 已安装" -ForegroundColor Red
    exit 1
}

# 1. 先检查端口是否已经被 Edge 占用 (复用)
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 3
    if ($resp.StatusCode -eq 200) {
        $ver = ($resp.Content | ConvertFrom-Json).Browser
        Write-Host "[OK] Edge CDP 已经在端口 $Port 运行 ($ver), 直接复用" -ForegroundColor Green
        exit 0
    }
} catch {
    # 端口没监听, 继续往下启动
}

# 2. 确保 user-data-dir 存在
if (-not (Test-Path $UserDataDir)) {
    Write-Host "[INFO] 创建 user-data-dir: $UserDataDir"
    New-Item -ItemType Directory -Path $UserDataDir -Force | Out-Null
}

# 3. 启动 Edge (detached, 不阻塞当前 shell)
Write-Host "[INFO] 启动 Edge: $EdgePath" -ForegroundColor Cyan
Write-Host "       Port:          $Port"
Write-Host "       User-data-dir: $UserDataDir"
Write-Host "       Start URL:     $StartUrl"
Write-Host ""

$args = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$UserDataDir",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=TranslateUI",
    $StartUrl
)

Start-Process -FilePath $EdgePath -ArgumentList $args

# 4. 等待端口就绪 (最多 15 秒)
Write-Host "[INFO] 等待 CDP 端口就绪..." -NoNewline
$ready = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Write-Host "." -NoNewline
    }
}
Write-Host ""

if ($ready) {
    $ver = ($resp.Content | ConvertFrom-Json).Browser
    Write-Host "[OK] Edge CDP 已就绪 ($ver)" -ForegroundColor Green
    Write-Host ""
    Write-Host "首次使用: 在 Edge 窗口里扫码登录小土豆炒股账号"
    Write-Host "之后 cookies 会持久化到: $UserDataDir"
    Write-Host "以后每次启动都会自动加载登录状态"
} else {
    Write-Host "[WARN] Edge 启动了但 15 秒内 CDP 端口还没就绪" -ForegroundColor Yellow
    Write-Host "       可能是 user-data-dir 被锁, 或 Edge 版本问题"
    Write-Host "       手动验证: curl http://127.0.0.1:$Port/json/version"
    exit 2
}
