# run.ps1 - one-shot runner for the new version (start.ps1 is left untouched).
# Run this as a script file; do not paste its lines into the console.
#
# Usage:
#   .\run.ps1                                    # web UI on 127.0.0.1:8765, data dir data/arena
#   .\run.ps1 demo --duration-ms 22000           # short demo match (quick look at opening supply)
#   .\run.ps1 demo --output runs/demo-first      # full 180s demo match
#   .\run.ps1 replay runs/demo-first             # verify a replay bundle
#   .\run.ps1 match --red examples/stack_builder.py --blue examples/adaptive_raider.py --output runs/my-match
#   .\run.ps1 match --red examples/stack_builder.py --blue examples/adaptive_raider.py --config my.json
#   .\run.ps1 series --red examples/stack_builder.py --blue examples/adaptive_raider.py --count 2 --output runs/my-pair
#   .\run.ps1 serve --port 8888                  # custom port
#
# All arguments are passed through to: python -m robocon_sim ...
# Requires Python 3.11+ (picked via the py launcher, avoiding old PATH python).

Set-Location -LiteralPath $PSScriptRoot

# ---- locate a Python 3.11+ interpreter ----
$exe = $null
$verArg = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($ver in @('-3.11', '-3.12', '-3.13', '-3.14', '-3')) {
        try {
            $vi = & py $ver -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$vi -ge [version]'3.11') {
                $exe = 'py'; $verArg = $ver; break
            }
        } catch { }
    }
}
if (-not $exe) {
    $vi = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and [version]$vi -ge [version]'3.11') { $exe = 'python' }
}
if (-not $exe) {
    Write-Error 'No Python 3.11+ interpreter found. Install Python 3.11 or newer and retry.'
    exit 1
}
Write-Host "Using Python: $exe $verArg"

# ---- build command line ----
$forward = @('-m', 'robocon_sim')
if ($args.Count -gt 0) {
    $forward += $args
    # serve requires an explicit --trusted-code flag; this runner is for
    # reviewed local code only, so add it automatically when missing.
    if ($args -contains 'serve' -and $args -notcontains '--trusted-code') {
        $forward += '--trusted-code'
        Write-Host 'Note: appended --trusted-code (local reviewed code only).'
    }
} else {
    $forward += @('serve', '--host', '127.0.0.1', '--port', '8765', '--data', 'data/arena', '--trusted-code')
}

if ($verArg) { & $exe $verArg @forward } else { & $exe @forward }
exit $LASTEXITCODE
