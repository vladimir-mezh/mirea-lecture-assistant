$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Python = if (Test-Path -LiteralPath $VenvPython) { $VenvPython } else { "python" }

# Avoid collecting unrelated DLLs injected into the interactive Codex PATH.
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"

& $Python -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m PyInstaller --clean --noconfirm MireaLectureAssistant.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Built: $ProjectRoot\dist\MireaLectureAssistant.exe"
