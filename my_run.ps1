# my_run.ps1 — 一次性启动器（不改动 start.ps1）。
# 用法：双击 run.cmd，或在 PowerShell 里执行下面这些命令。
#
#   .\my_run.ps1                              启动网页界面 127.0.0.1:8765，数据目录 data/arena
#   .\my_run.ps1 demo --duration-ms 22000     跑一场 22 秒的演示对局
#   .\my_run.ps1 replay runs/demo-first       校验一个比赛包
#   .\my_run.ps1 match --red ... --blue ...   单局
#   .\my_run.ps1 series --red ... --blue ...  换边双局
#   .\my_run.ps1 serve --port 8888            换端口
#
# 所有参数原样转发给：python -m robocon_sim ...
# 需要 Python 3.11 或更高版本（优先用 py 启动器，避开 PATH 里的旧 python）。

$ErrorActionPreference = 'Stop'
$script:exitCode = 1

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($ver in @('-3.11', '-3.12', '-3.13', '-3.14', '-3')) {
            try {
                $vi = & py $ver -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0 -and [version]$vi -ge [version]'3.11') { return @('py', $ver) }
            } catch { }
        }
    }
    try {
        $vi = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$vi -ge [version]'3.11') { return @('python', $null) }
    } catch { }
    return $null
}

try {
    if ($PSScriptRoot) { Set-Location -LiteralPath $PSScriptRoot }
    # Python 的中文输出要能被控制台正确显示，否则全是乱码
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $found = Find-Python
    if (-not $found) {
        Write-Host '找不到 Python 3.11 或更高版本。请先安装，然后重新运行。' -ForegroundColor Red
        Write-Host '  https://www.python.org/downloads/windows/' -ForegroundColor Red
        $script:exitCode = 1
    } else {
        $exe = $found[0]
        $verArg = $found[1]
        Write-Host ("使用 Python: {0} {1}" -f $exe, $verArg) -ForegroundColor DarkGray

        $forward = @('-m', 'robocon_sim')
        if ($args.Count -gt 0) {
            $forward += $args
            # serve 需要显式 --trusted-code；这个脚本只用于队内已审查代码，自动补上
            if ($args -contains 'serve' -and $args -notcontains '--trusted-code') {
                $forward += '--trusted-code'
                Write-Host '已自动追加 --trusted-code（仅限本机已审查代码）' -ForegroundColor DarkGray
            }
        } else {
            Write-Host '正在启动网页界面；停止请按 Ctrl+C。' -ForegroundColor DarkGray
            $forward += @('serve', '--host', '127.0.0.1', '--port', '8765', '--data', 'data/arena', '--trusted-code')
        }

        if ($verArg) { & $exe $verArg @forward } else { & $exe @forward }
        $script:exitCode = $LASTEXITCODE
    }
} catch {
    Write-Host ''
    Write-Host ('启动失败：' + $_.Exception.Message) -ForegroundColor Red
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    $script:exitCode = 1
}

# 只有 run.cmd 会设置 BR_PAUSE：双击时窗口不能一闪而过，从终端调用时不要卡住
if ($script:exitCode -ne 0 -and $env:BR_PAUSE -eq '1') {
    Write-Host ''
    Read-Host '按回车关闭窗口'
}
exit $script:exitCode
