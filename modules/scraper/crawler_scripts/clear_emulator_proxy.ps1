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

& $adb @serialArgs shell settings delete global http_proxy
& $adb @serialArgs shell settings delete global global_http_proxy_host
& $adb @serialArgs shell settings delete global global_http_proxy_port
& $adb @serialArgs shell settings delete global global_http_proxy_exclusion_list
& $adb @serialArgs reverse --remove "tcp:$Port"

Write-Host "Proxy cleared."
