# AriaStreamLab bootstrap (Windows PowerShell): a Python environment the SDK wheels support,
# AriaStreamLab installed into it, and the application started.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Sdk      # also Meta's SDK
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoApp    # do not start the app
param([switch]$Sdk, [switch]$NoApp)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = $null
foreach ($v in @("3.12", "3.11", "3.10")) {
  try {
    $out = & py "-$v" -c "import sys; print(sys.version.split()[0])" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = "py -$v"; Write-Host "using Python $out"; break }
  } catch {}
}
if (-not $py) {
  Write-Error "No Python 3.10-3.12 found (the versions Meta's SDK supports). Install one from python.org and run this script again."
}
if (-not (Test-Path ".venv")) { Invoke-Expression "$py -m venv .venv" }
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip | Out-Null
if ($Sdk) { python -m pip install -e ".[aria]" } else { python -m pip install -e . }
Write-Host ""
ariastreamlab doctor
Write-Host ""
Write-Host "Environment ready: '.\.venv\Scripts\Activate.ps1' and then 'ariastreamlab' opens the application."
if (-not $NoApp) { ariastreamlab }
