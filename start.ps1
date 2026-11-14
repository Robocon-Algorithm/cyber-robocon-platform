param([int]$Port = 8765)
Set-Location -LiteralPath $PSScriptRoot
python -m robocon_sim serve --host 127.0.0.1 --port $Port --trusted-code
