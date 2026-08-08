# ============ 茶辑机器人 · 飞书配置助手（lark-cli + nginx 版） ============
# 使用飞书官方 CLI 工具 lark-cli 进行配置验证
# 使用 nginx 反向代理提供外网访问（端口 8080）

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "   茶辑 机器人 · 飞书配置助手" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# ---------- 1. 读取配置 ----------
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Error ".env 文件不存在于 $Root"
    exit 1
}
$EnvContent = Get-Content $EnvFile -Raw
$FEISHU_APP_ID = [regex]::Match($EnvContent, 'FEISHU_APP_ID=([^\r\n]+)').Groups[1].Value.Trim()
$FEISHU_APP_SECRET = [regex]::Match($EnvContent, 'FEISHU_APP_SECRET=([^\r\n]+)').Groups[1].Value.Trim()
Write-Host "[OK] 读取配置: APP_ID=$FEISHU_APP_ID" -ForegroundColor Green

# ---------- 2. 检查 lark-cli ----------
Write-Host ""
Write-Host "[2/7] 检查飞书 CLI (lark-cli)..." -ForegroundColor Yellow
$larkCli = Get-Command lark-cli -ErrorAction SilentlyContinue
if (-not $larkCli) {
    Write-Host "  lark-cli 未安装，正在安装..." -ForegroundColor Yellow
    npm install -g @larksuite/cli 2>&1 | Out-Null
}
$cliVer = & lark-cli --version 2>$null
if ($cliVer) {
    Write-Host "[OK] lark-cli 已安装: $cliVer" -ForegroundColor Green
} else {
    Write-Warning "lark-cli 安装失败，请手动运行: npm install -g @larksuite/cli"
}

# 配置 lark-cli（如果未配置）
$configShow = & lark-cli config show 2>$null
if ($configShow -notmatch $FEISHU_APP_ID) {
    Write-Host "  正在配置 lark-cli..." -ForegroundColor Yellow
    $secret = $FEISHU_APP_SECRET
    echo $secret | lark-cli config init --app-id $FEISHU_APP_ID --app-secret-stdin --brand feishu 2>&1 | Out-Null
    Write-Host "[OK] lark-cli 配置完成" -ForegroundColor Green
} else {
    Write-Host "[OK] lark-cli 已配置" -ForegroundColor Green
}

# 验证凭证
Write-Host "  验证飞书凭证..." -ForegroundColor Yellow
$authStatus = & lark-cli auth status 2>$null | ConvertFrom-Json
if ($authStatus.identities.bot.status -eq 'ready') {
    Write-Host "[OK] Bot 身份验证成功" -ForegroundColor Green
} else {
    Write-Error "Bot 身份验证失败，请检查 APP_ID 和 APP_SECRET"
    exit 1
}

# ---------- 3. 获取公网 URL ----------
Write-Host ""
Write-Host "[3/7] 获取外网访问地址..." -ForegroundColor Yellow
# 获取本机公网 IP
try {
    $publicIp = (Invoke-RestMethod -Uri "https://api.ipify.org?format=text" -TimeoutSec 10).Trim()
    Write-Host "  公网 IP: $publicIp" -ForegroundColor White
} catch {
    $publicIp = "<你的公网IP>"
    Write-Warning "无法自动获取公网 IP，请手动填写"
}
$WebhookUrl = "http://${publicIp}:8080/feishu/webhook"
$AppUrl = "http://${publicIp}:8080"
Write-Host "[OK] 外网访问地址: $AppUrl" -ForegroundColor Green
Write-Host "     Webhook: $WebhookUrl" -ForegroundColor Cyan

# ---------- 4. 验证 Docker 服务 ----------
Write-Host ""
Write-Host "[4/7] 验证 Docker 服务..." -ForegroundColor Yellow
Set-Location $Root
$containerStatus = docker inspect --format '{{.State.Health.Status}}' vedio-agent-app 2>$null
if ($containerStatus -eq 'healthy') {
    Write-Host "[OK] Docker 服务运行中 (healthy)" -ForegroundColor Green
} else {
    Write-Host "  启动 Docker 服务..." -ForegroundColor Yellow
    docker compose up -d --build 2>&1 | Out-Null
    Start-Sleep -Seconds 15
    $containerStatus = docker inspect --format '{{.State.Health.Status}}' vedio-agent-app 2>$null
    if ($containerStatus -eq 'healthy') {
        Write-Host "[OK] Docker 服务已启动 (healthy)" -ForegroundColor Green
    } else {
        Write-Warning "Docker 服务未就绪，请稍后重试"
    }
}

# ---------- 5. 验证 webhook ----------
Write-Host ""
Write-Host "[5/7] 验证本地 webhook 联通性..." -ForegroundColor Yellow
try {
    $challenge = "test_" + [guid]::NewGuid().ToString('N').Substring(0,8)
    $testBody = @{ type = "url_verification"; challenge = $challenge; token = "" } | ConvertTo-Json
    $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8080/feishu/webhook" -Method Post `
              -Body $testBody -ContentType "application/json" -TimeoutSec 10
    if ($resp.challenge -eq $challenge) {
        Write-Host "[OK] 本地 webhook 验证通过" -ForegroundColor Green
    } else {
        Write-Warning "本地 webhook 响应异常"
    }
} catch {
    Write-Warning "本地 webhook 验证失败：$($_.Exception.Message)"
}

# ---------- 6. 生成权限申请链接 ----------
Write-Host ""
Write-Host "[6/7] ===== 飞书开放平台配置步骤 =====" -ForegroundColor Magenta
Write-Host ""

# 权限申请一键链接
$scopes = "im:message,im:message:send_as_bot,im:resource,im:chat,im:chat:readonly,drive:drive,drive:file:write,contact:user.id:readonly"
$scopeUrl = "https://open.feishu.cn/page/scope-apply?clientID=$FEISHU_APP_ID&scopes=$scopes"

Write-Host "应用管理页面: https://open.feishu.cn/app/$FEISHU_APP_ID" -ForegroundColor White
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Gray
Write-Host ""
Write-Host "【步骤 1 · 一键申请权限】" -ForegroundColor Yellow
Write-Host "  点击下方链接一键申请全部所需权限：" -ForegroundColor White
Write-Host "  $scopeUrl" -ForegroundColor Cyan
Write-Host ""
Write-Host "  所需权限列表：" -ForegroundColor White
Write-Host "    im:message              获取与发送单聊、群组消息" -ForegroundColor Gray
Write-Host "    im:message:send_as_bot  以应用身份发送消息" -ForegroundColor Gray
Write-Host "    im:resource             获取用户上传的图片/文件" -ForegroundColor Gray
Write-Host "    im:chat                 群聊操作" -ForegroundColor Gray
Write-Host "    im:chat:readonly        读取群聊信息" -ForegroundColor Gray
Write-Host "    drive:drive             云盘访问" -ForegroundColor Gray
Write-Host "    drive:file:write        上传文件到云盘" -ForegroundColor Gray
Write-Host "    contact:user.id:readonly 通过手机号获取用户ID" -ForegroundColor Gray
Write-Host ""
Write-Host "【步骤 2 · 启用机器人】" -ForegroundColor Yellow
Write-Host "  左侧菜单 → 应用功能 → 机器人 → 启用" -ForegroundColor White
Write-Host "  机器人名称填：茶辑" -ForegroundColor White
Write-Host ""
Write-Host "【步骤 3 · 配置事件订阅】" -ForegroundColor Yellow
Write-Host "  左侧菜单 → 事件订阅" -ForegroundColor White
Write-Host "  请求地址填写：" -ForegroundColor White
Write-Host "    $WebhookUrl" -ForegroundColor Cyan
Write-Host "  添加事件 → 接收消息 (im.message.receive_v1)" -ForegroundColor White
Write-Host ""
Write-Host "  注意：路由器需配置端口转发（外部端口→192.168.x.x:8080）" -ForegroundColor Gray
Write-Host ""
Write-Host "【步骤 4 · 创建版本并发布】" -ForegroundColor Yellow
Write-Host "  左侧菜单 → 版本管理与发布 → 创建版本" -ForegroundColor White
Write-Host "  版本号 0.1.0 → 保存 → 申请发布" -ForegroundColor White
Write-Host "  （企业自建应用不需要审批，立即生效）" -ForegroundColor Green
Write-Host ""
Write-Host "【步骤 5 · 测试机器人】" -ForegroundColor Yellow
Write-Host "  打开飞书 → 搜索「茶辑」→ 进入对话" -ForegroundColor White
Write-Host "  发送：帮助 → 查看帮助文本" -ForegroundColor White
Write-Host "  发送视频文件 → 自动处理 → 收到成品视频" -ForegroundColor White
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Gray

# ---------- 7. lark-cli 测试命令 ----------
Write-Host ""
Write-Host "[7/7] ===== lark-cli 常用命令 =====" -ForegroundColor Magenta
Write-Host ""
Write-Host "  # 检查认证状态" -ForegroundColor Gray
Write-Host "  lark-cli auth status" -ForegroundColor White
Write-Host ""
Write-Host "  # 健康检查" -ForegroundColor Gray
Write-Host "  lark-cli doctor" -ForegroundColor White
Write-Host ""
Write-Host "  # 发送测试消息（需先获取 open_id）" -ForegroundColor Gray
Write-Host "  lark-cli im +messages-send --receive-id <open_id> --text '测试'" -ForegroundColor White
Write-Host ""
Write-Host "  # 上传文件到飞书云盘" -ForegroundColor Gray
Write-Host "  lark-cli drive +upload --file <文件路径>" -ForegroundColor White
Write-Host ""
Write-Host "  # 用户授权（获取个人数据访问权限）" -ForegroundColor Gray
Write-Host "  lark-cli auth login --recommend" -ForegroundColor White
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  配置完成后，在飞书中搜索「茶辑」即可使用" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
