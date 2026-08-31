param(
    [switch]$Once,
    [switch]$SetupOnly,
    [switch]$SkipInstall,
    [switch]$PrintQuotes,
    [switch]$NoNotify,
    [switch]$DebugLog,
    [switch]$IgnoreMarketHours,
    [switch]$ConsoleMode,
    [switch]$NoBrowser,
    [switch]$NoWidget,
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = "1"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

Write-Step "检查 Python 版本"
$PreviousErrorAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$PythonMode = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $PythonMode = "python" }
}
if (-not $PythonMode -and (Get-Command py -ErrorAction SilentlyContinue)) {
    & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $PythonMode = "py" }
}
$ErrorActionPreference = $PreviousErrorAction
if (-not $PythonMode) {
    throw "未找到可用的 Python 3.10+。请安装 Python，并勾选 Add Python to PATH。"
}

function Invoke-SystemPython {
    param([string[]]$PythonArgs)
    if ($PythonMode -eq "py") {
        & py -3 @PythonArgs
    } else {
        & python @PythonArgs
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python 命令执行失败，退出码: $LASTEXITCODE"
    }
}

$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $ProjectDir "requirements.txt"
$ConfigPath = Join-Path $ProjectDir "config.json"
$ExampleConfig = Join-Path $ProjectDir "config.example.json"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Step "首次运行：创建 Python 虚拟环境"
    Invoke-SystemPython -PythonArgs @("-m", "venv", $VenvDir)
}

if (-not $SkipInstall) {
    $PreviousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $VenvPython -c "import requests, tzdata" 2>$null
    $DependenciesReady = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $PreviousErrorAction
    if (-not $DependenciesReady) {
        Write-Step "安装运行依赖"
        & $VenvPython -m pip install --disable-pip-version-check -r $Requirements
        if ($LASTEXITCODE -ne 0) {
            throw "依赖安装失败，请检查网络后重新运行。"
        }
    } else {
        Write-Host "依赖已就绪。" -ForegroundColor DarkGreen
    }
}

$ConfigWasCreated = $false
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Write-Step "首次运行：生成默认配置"
    Copy-Item -LiteralPath $ExampleConfig -Destination $ConfigPath
    $ConfigWasCreated = $true
    Write-Host "已生成示例自选股，稍后可直接在网页中修改。" -ForegroundColor Yellow
}

Write-Step "校验配置"
& $VenvPython main.py --config $ConfigPath --validate-config
if ($LASTEXITCODE -ne 0) {
    throw "config.json 校验失败，请修改后重试。"
}

if ($SetupOnly) {
    Write-Host "初始化完成。以后双击 start.cmd 即可启动。" -ForegroundColor Green
    exit 0
}

$UseConsole = $ConsoleMode -or $Once -or $PrintQuotes -or $NoNotify -or $IgnoreMarketHours
if (-not $UseConsole) {
    $WebArgs = @("web.py", "--config", $ConfigPath, "--port", $Port.ToString())
    if (-not $ConfigWasCreated) { $WebArgs += "--auto-start" }
    if ($NoBrowser) { $WebArgs += "--no-browser" }
    if (-not $NoWidget) { $WebArgs += "--widget" }
    if ($DebugLog) { $WebArgs += "--debug" }
    Write-Step "启动可视化控制台（关闭本窗口即可退出）"
    & $VenvPython @WebArgs
    exit $LASTEXITCODE
}

$RunArgs = @("main.py", "--config", $ConfigPath)
if ($Once) { $RunArgs += "--once" }
if ($PrintQuotes) { $RunArgs += "--print-quotes" }
if ($NoNotify) { $RunArgs += "--no-notify" }
if ($DebugLog) { $RunArgs += "--debug" }
if ($IgnoreMarketHours) { $RunArgs += "--ignore-market-hours" }

Write-Step $(if ($Once) { "单次获取行情" } else { "启动命令行实时监控（按 Ctrl+C 停止）" })
& $VenvPython @RunArgs
exit $LASTEXITCODE
