# AriaStreamLab bootstrap (Windows PowerShell): a Python environment the SDK wheels support,
# AriaStreamLab installed into it, and the application started.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoApp    # do not start the app
#
# Meta's packages (client SDK, projectaria_tools) have no Windows build: on Windows
# AriaStreamLab designs experiments, runs synthetic sessions and opens sessions recorded
# elsewhere, but cannot stream from the glasses or replay their VRS. -Sdk is refused.
param([switch]$Sdk, [switch]$NoApp)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

# Print a message and stop with an exit code (Write-Error would end the script before `exit`).
function Fail([string]$Message, [int]$Code = 1) {
  [Console]::Error.WriteLine($Message)
  exit $Code
}

# $ErrorActionPreference does not apply to native programs: check every exit code.
function Invoke-Checked([string]$What, [scriptblock]$Command) {
  & $Command
  if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE); the environment is not ready." }
}

if ($Sdk) {
  Fail ("Meta's client SDK (projectaria-client-sdk) has no Windows build: streaming " +
    "from the glasses needs macOS (Apple Silicon) or Linux. Run without -Sdk to install " +
    "AriaStreamLab for designing experiments, synthetic sessions and opening sessions.") 2
}

$py = $null
foreach ($v in @("3.12", "3.11", "3.10")) {
  try {
    $out = & py "-$v" -c "import sys; print(sys.version.split()[0])" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = "py -$v"; Write-Host "using Python $out"; break }
  } catch {}
}
if (-not $py) {
  Fail "No Python 3.10-3.12 found (the versions AriaStreamLab supports). Install one from python.org and run this script again."
}
if (Test-Path ".venv") {
  # an existing environment is reused only if it runs a supported version
  $venvVer = "broken"
  try {
    $out = & .\.venv\Scripts\python.exe -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ($LASTEXITCODE -eq 0) { $venvVer = "$out".Trim() }
  } catch {}
  if (@("3.10", "3.11", "3.12") -notcontains $venvVer) {
    $what = if ($venvVer -eq "broken") { "has no working Python" } else { "runs Python $venvVer, not 3.10-3.12" }
    Fail ("The existing .venv $what. Remove it (Remove-Item -Recurse -Force .venv) and " +
      "run this script again.")
  }
  Write-Host "reusing .venv (Python $venvVer)"
} else {
  Invoke-Checked "creating .venv" { Invoke-Expression "$py -m venv .venv" }
}
& .\.venv\Scripts\Activate.ps1
Invoke-Checked "upgrading pip" { python -m pip install --upgrade pip | Out-Null }
Invoke-Checked "installing AriaStreamLab" { python -m pip install -e . }
Write-Host ""
ariastreamlab doctor  # informative: a warning here does not stop the script
Write-Host ""
Write-Host "Environment ready: '.\.venv\Scripts\Activate.ps1' and then 'ariastreamlab' opens the application."
if (-not $NoApp) { ariastreamlab }
