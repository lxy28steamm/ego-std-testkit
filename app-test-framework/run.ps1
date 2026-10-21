param([switch]$Device, [string]$Config = "$PSScriptRoot/config/android.yaml")
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $runnerArgs = @('-m', 'pytest', '--app-config', $Config)
    if ($Device) { $runnerArgs += @('--device', '-m', 'device') }
    & "$PSScriptRoot/.venv/Scripts/python.exe" @runnerArgs
    $resultCode = $LASTEXITCODE
} finally { Pop-Location }
exit $resultCode
