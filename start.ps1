#!/usr/bin/env pwsh
# 启动脚本（Windows PowerShell）：在 conda 环境 vedioDealEnv 中启动 Web 服务
param(
    [switch]$BuildSandboxImage,   # 是否先构建沙箱 worker 镜像
    [switch]$InstallDeps          # 首次运行时用，安装依赖
)

$ErrorActionPreference = "Stop"
$CondaEnvName = "vedioDealEnv"

# 定位 conda
function Get-CondaPath {
    # 常见安装位置
    $candidates = @(
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        "$env:USERPROFILE\miniforge3\Scripts\conda.exe",
        "C:\ProgramData\miniconda3\Scripts\conda.exe",
        "C:\ProgramData\anaconda3\Scripts\conda.exe"
    )
    foreach ($p in $candidates) { if (Test-Path $p) { return $p } }
    $condaInPath = Get-Command conda -ErrorAction SilentlyContinue
    if ($condaInPath) { return $condaInPath.Source }
    throw "找不到 conda，请先安装 Miniconda / Anaconda"
}

$conda = Get-CondaPath
Write-Host "▶ 使用 conda: $conda" -ForegroundColor Cyan

# 检查并创建环境
$envList = & $conda env list
if ($envList -match "^\s*$CondaEnvName\s") {
    Write-Host "✔ conda 环境 $CondaEnvName 已存在" -ForegroundColor Green
} else {
    Write-Host "▶ 创建 conda 环境: $CondaEnvName (Python 3.11)" -ForegroundColor Cyan
    & $conda create -n $CondaEnvName -y python=3.11 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "conda 环境创建失败" }
}

# 在环境里安装依赖
$pyExe = & $conda run -n $CondaEnvName python -c "import sys; print(sys.executable)" 2>$null
if (-not $pyExe) { $pyExe = (& $conda info --base 2>$null) + "\envs\$CondaEnvName\python.exe" }
Write-Host "▶ Python: $pyExe" -ForegroundColor Cyan

if ($InstallDeps) {
    Write-Host "▶ 安装依赖（pip install -r requirements.txt）" -ForegroundColor Cyan
    & $conda run -n $CondaEnvName pip install -r requirements.txt | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败" }
    # Windows 额外：确认 ffmpeg 在 PATH
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $ff) { Write-Warning "本机 PATH 中未找到 ffmpeg。若 SANDBOX_ENABLED=false 则需安装 ffmpeg 到系统。" }
}

# 构建沙箱 worker 镜像
if ($BuildSandboxImage) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "未找到 docker 命令，无法构建沙箱镜像"
    }
    Write-Host "▶ 构建沙箱 worker 镜像 vedio-agent-worker:latest" -ForegroundColor Cyan
    docker build -t vedio-agent-worker:latest -f docker/worker.Dockerfile .
    if ($LASTEXITCODE -ne 0) { throw "沙箱镜像构建失败" }
}

# 拷贝 .env.example -> .env（如不存在）
if (-not (Test-Path .env)) {
    Write-Host "▶ 首次运行：从 .env.example 生成 .env（请按需修改飞书配置）" -ForegroundColor Yellow
    Copy-Item .env.example .env
}

Write-Host "▶ 启动 Web 服务，端口 8000" -ForegroundColor Green
Write-Host "  打开 http://localhost:8000 访问 UI" -ForegroundColor Gray
& $conda run -n $CondaEnvName python -m uvicorn app.web.main:app --host 0.0.0.0 --port 8000 --reload
