# 肥鱼五层记忆 · worker 启停
#
# 用法（在 qqbot 目录下）：
#   .\tools\fishmem_workers.ps1 start      # 全部后台启动
#   .\tools\fishmem_workers.ps1 stop
#   .\tools\fishmem_workers.ps1 status
#   .\tools\fishmem_workers.ps1 once       # 各跑一轮就退（排错用）
#   .\tools\fishmem_workers.ps1 eval       # 只跑人格不变性自检
#
# 三个 worker 互相独立，一个挂了不影响别的。日志在
#   data\plugin_data\astrbot_plugin_fish_memory\logs\

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "once", "eval", "health")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

$Qqbot = Split-Path -Parent $PSScriptRoot
$Plugin = Join-Path $Qqbot "data\plugins\astrbot_plugin_fish_memory"
$Worker = Join-Path $Plugin "worker"
$Python = Join-Path $Qqbot ".venv\Scripts\python.exe"
$PidDir = Join-Path $Qqbot "data\plugin_data\astrbot_plugin_fish_memory\logs\pids"
$LogDir = Join-Path $Qqbot "data\plugin_data\astrbot_plugin_fish_memory\logs"

if (-not (Test-Path $Python)) { throw "找不到 python: $Python" }
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Workers = @{
    "ingest" = "ingest_worker.py"
    "life"   = "life_daemon.py"
    "digest" = "daily_digest.py"
}

function Get-PidFile($name) { Join-Path $PidDir "$name.pid" }

function Start-Worker($name) {
    $pidFile = Get-PidFile $name
    if (Test-Path $pidFile) {
        $old = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
            Write-Host "  $name 已在运行 (pid $old)"
            return
        }
    }
    $script = Join-Path $Worker $Workers[$name]
    $log = Join-Path $LogDir "$name.out.log"
    $p = Start-Process -FilePath $Python -ArgumentList @($script) `
        -WorkingDirectory $Plugin -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    $p.Id | Set-Content $pidFile
    Write-Host "  $name 启动 pid=$($p.Id)  log=$log"
}

function Stop-Worker($name) {
    $pidFile = Get-PidFile $name
    if (-not (Test-Path $pidFile)) { Write-Host "  $name 没在跑"; return }
    $old = Get-Content $pidFile -ErrorAction SilentlyContinue
    if ($old) {
        $proc = Get-Process -Id $old -ErrorAction SilentlyContinue
        if ($proc) {
            Stop-Process -Id $old -Force
            Write-Host "  $name 已停止 (pid $old)"
        } else {
            Write-Host "  $name pid $old 已不存在"
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

switch ($Action) {
    "start"  { $Workers.Keys | ForEach-Object { Start-Worker $_ } }
    "stop"   { $Workers.Keys | ForEach-Object { Stop-Worker $_ } }
    "status" {
        foreach ($n in $Workers.Keys) {
            $pidFile = Get-PidFile $n
            $state = "未运行"
            if (Test-Path $pidFile) {
                $old = Get-Content $pidFile -ErrorAction SilentlyContinue
                if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
                    $state = "运行中 pid=$old"
                }
            }
            Write-Host ("  {0,-8} {1}" -f $n, $state)
        }
    }
    "once" {
        foreach ($n in $Workers.Keys) {
            Write-Host "--- $n ---"
            & $Python (Join-Path $Worker $Workers[$n]) --once
        }
    }
    "eval" {
        & $Python (Join-Path $Worker "eval_runner.py")
        exit $LASTEXITCODE
    }
    "health" {
        & $Python (Join-Path $Worker "daily_digest.py") --once
        $rep = Join-Path $Qqbot "data\plugin_data\astrbot_plugin_fish_memory\reports\health-latest.md"
        if (Test-Path $rep) { Get-Content $rep -Encoding UTF8 | Write-Host }
    }
}
