# 在 Windows 上一键备齐打包环境并产出 sshdesk.exe
#
#   用法（PowerShell，普通用户即可，不需要管理员）：
#     powershell -ExecutionPolicy Bypass -File tools\bootstrap_windows.ps1
#     powershell -ExecutionPolicy Bypass -File tools\bootstrap_windows.ps1 -OneFile
#
# 做的事：查 Python/git → clone 或更新 4 个仓库 → 建 venv → 装依赖 → PyInstaller 出包。
# 产物：<Root>\sshdesk\dist\sshdesk\sshdesk.exe（-OneFile 则是 dist\sshdesk.exe）
#
# 注意：PyInstaller 不能交叉编译，本脚本只能在 Windows 上跑。

param(
    [string]$Root = "$env:USERPROFILE\work\repos",
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$repos = @(
    "https://github.com/yumoqing/sshdesk.git",
    "https://github.com/yumoqing/appPublic.git",
    "https://github.com/yumoqing/bricks-qt.git",
    "https://github.com/yumoqing/pylice.git"
)

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "缺 git：https://git-scm.com/download/win 装完重开 PowerShell"
}

# ---- Python 3.12（PySide6 6.6+ 支持到 3.12 最稳） ----
$py = $null
foreach ($cand in @("py -3.12", "python")) {
    $exe, $pre = $cand.Split(" ")
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $ver = & $exe $pre -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($ver -match "^3\.(9|1[0-3])$") { $py = $cand; break }
    }
}
if (-not $py) {
    throw "缺 Python 3.9+（建议 3.12）：winget install Python.Python.3.12 装完重开 PowerShell"
}
Step "Python: $py ($ver)"

# ---- 仓库 ----
New-Item -ItemType Directory -Force -Path $Root | Out-Null
foreach ($url in $repos) {
    $name = [IO.Path]::GetFileNameWithoutExtension($url)
    $dir = Join-Path $Root $name
    if (Test-Path (Join-Path $dir ".git")) {
        Step "更新 $name"
        & git -C $dir fetch --all --prune
        & git -C $dir pull --ff-only
    } else {
        Step "克隆 $name"
        & git clone --depth 1 $url $dir
    }
    if ($LASTEXITCODE -ne 0) { throw "$name 拉取失败（网络/证书？）" }
}

$sshdesk = Join-Path $Root "sshdesk"
$venv = Join-Path $Root ".venvs\sshdesk-build"
$vpy = Join-Path $venv "Scripts\python.exe"

if (-not (Test-Path $vpy)) {
    Step "建 venv: $venv"
    $parts = $py.Split(" ")
    & $parts[0] $parts[1..($parts.Length - 1)] -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "建 venv 失败" }
}

Step "装依赖（index: $IndexUrl）"
& $vpy -m pip install --upgrade pip -i $IndexUrl
& $vpy -m pip install -i $IndexUrl "PySide6-Essentials>=6.6" "paramiko>=3.4" "pyte>=0.8" wcwidth asyncssh jinja2 pyinstaller
& $vpy -m pip install -i $IndexUrl -e (Join-Path $Root "pylice") -e (Join-Path $Root "appPublic")
& $vpy -m pip install -i $IndexUrl -e $sshdesk --no-deps
if ($LASTEXITCODE -ne 0) { throw "装依赖失败" }

# ---- 打包 ----
$env:SSHDESK_APPPUBLIC = Join-Path $Root "appPublic"
$env:SSHDESK_BRICKSQT = Join-Path $Root "bricks-qt"
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
Step "PyInstaller $mode"
& $vpy (Join-Path $sshdesk "tools\build_exe.py") $mode
if ($LASTEXITCODE -ne 0) { throw "打包失败" }

$out = if ($OneFile) { Join-Path $sshdesk "dist\sshdesk.exe" } else { Join-Path $sshdesk "dist\sshdesk\sshdesk.exe" }
if (-not (Test-Path $out)) { throw "没找到产物：$out" }

Step "冒烟（窗口 1.2 秒后自动退出）"
# --windowed 版是 GUI 程序，直接用 & 调不会等它退出，必须 Start-Process -Wait 拿退出码
$proc = Start-Process -FilePath $out -ArgumentList "--selftest" -Wait -PassThru
if ($proc.ExitCode -ne 0) { throw "--selftest 失败（rc=$($proc.ExitCode)）" }

Write-Host ""
Write-Host "产物  : $out" -ForegroundColor Green
Write-Host "机器码: 打开窗口点工具栏“授权”查看；或用 --console 版跑：$out --license" -ForegroundColor Green
Write-Host "        （机器码必须在这台目标机上生成：MachineGuid + 网卡MAC + 系统盘序列号）"