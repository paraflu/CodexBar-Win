<# CodexBar for Windows - Quick Setup & Launch #>

Write-Host ""
Write-Host "  ╔═══════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "  ║      CodexBar for Windows v1.0.0      ║" -ForegroundColor Magenta
Write-Host "  ║   Claude Code & Codex Monitor           ║" -ForegroundColor Magenta
Write-Host "  ╚═══════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

# uv owns the environment: it also fetches Python if the machine has none
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "[!] uv not found. Install it: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

Write-Host "[*] uv found: $($uv.Source)" -ForegroundColor Green

Write-Host "[*] Syncing dependencies..." -ForegroundColor Cyan
uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[*] Dependencies installed." -ForegroundColor Green

# Create config directory
$configDir = "$env:USERPROFILE\.codexbar"
if (-not (Test-Path $configDir)) {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    Write-Host "[*] Config directory created: $configDir" -ForegroundColor Green
}

# Launch
Write-Host ""
Write-Host "[*] Launching CodexBar..." -ForegroundColor Yellow
Write-Host "    Right-click the tray icon to open menu" -ForegroundColor DarkGray
Write-Host "    Double-click to open the usage panel" -ForegroundColor DarkGray
Write-Host ""

uv run codexbar.py
