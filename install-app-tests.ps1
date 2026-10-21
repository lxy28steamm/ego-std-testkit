param([string]$BasePython = "$PSScriptRoot/.venv-gui/Scripts/python.exe")
$ErrorActionPreference = 'Stop'
$env:PYTHONPATH = ''
$frameworkRoot = Join-Path $PSScriptRoot 'app-test-framework'
$runnerPython = Join-Path $frameworkRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $runnerPython)) {
    & $BasePython -m venv (Join-Path $frameworkRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the APP test environment.' }
}
& $runnerPython -m pip install -r (Join-Path $frameworkRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install APP test dependencies.' }
Push-Location $frameworkRoot
try {
    & $runnerPython -m pytest tests/test_config.py tests/test_flow_helpers.py -q
    if ($LASTEXITCODE -ne 0) { throw 'APP test environment verification failed.' }
} finally {
    Pop-Location
}
Write-Host 'APP test framework ready. Start the workbench and select APP patrol.'
