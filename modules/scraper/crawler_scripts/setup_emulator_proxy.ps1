param(
    [string]$Serial = "",
    [int]$Port = 8080
)

$localAdb = Join-Path (Split-Path $PSScriptRoot -Parent) "platform-tools\adb.exe"
$adb = if (Test-Path $localAdb) { $localAdb } else { "adb" }
$serialArgs = @()
if ($Serial -ne "") {
    $serialArgs = @("-s", $Serial)
}

& $adb @serialArgs reverse "tcp:$Port" "tcp:$Port"
& $adb @serialArgs shell settings put global http_proxy "127.0.0.1:$Port"

Write-Host "Proxy configured: Android -> 127.0.0.1:$Port via adb reverse"
Write-Host "Run mitmdump/mitmproxy on this computer at port $Port."
