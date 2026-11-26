param([Parameter(Mandatory=$true)][string]$Serial,
      [ValidateSet('guest','usb','settings','tasks')][string]$Suite='guest')
$ErrorActionPreference='Stop'
$oldSerial=$env:ANDROID_UDID
Push-Location $PSScriptRoot
try {
    $env:ANDROID_UDID=$Serial
    $files=@{guest='tests/test_livflow_guest.py';usb='tests/test_livflow_usb.py';settings='tests/test_livflow_workflows.py';tasks='tests/test_livflow_workflows.py'}
    & "$PSScriptRoot/.venv/Scripts/python.exe" -m pytest $files[$Suite] --device --release-230 -m "flow_$Suite"
    $resultCode=$LASTEXITCODE
} finally {
    $env:ANDROID_UDID=$oldSerial
    Pop-Location
}
exit $resultCode
