<# Build dist\CodexBar.exe (single file, no console) with uv + PyInstaller #>

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[!] uv not found. Install it: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}

Write-Host "[*] Syncing the environment..." -ForegroundColor Cyan
uv sync --group dev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# customtkinter ships themes/fonts and winpty ships dll+exe helpers: without
# these the frozen app starts and then dies on the first widget or PTY call.
$pyiArgs = @(
    '--noconfirm', '--clean',
    '--onefile', '--windowed',
    '--name', 'CodexBar',
    '--icon', 'assets/codexbar.ico',
    '--add-data', 'assets;assets',
    '--collect-data', 'customtkinter',
    '--collect-all', 'winpty',
    '--hidden-import', 'pystray._win32',
    'codexbar.py'
)

Write-Host "[*] Building the exe..." -ForegroundColor Cyan
uv run pyinstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[ok] $PSScriptRoot\dist\CodexBar.exe" -ForegroundColor Green
