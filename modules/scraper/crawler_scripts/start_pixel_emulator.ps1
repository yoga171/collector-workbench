param(
    [string]$AvdName = "Pixel_Douyin_API35",
    [string]$Gpu = "software"
)

$root = Split-Path $PSScriptRoot -Parent
$sdk = Join-Path $root "android-sdk"
$emulator = Join-Path $sdk "emulator\emulator.exe"

$env:ANDROID_SDK_ROOT = $sdk
$env:ANDROID_HOME = $sdk
$env:ANDROID_EMULATOR_DISABLE_VULKAN = "1"

Start-Process -FilePath $emulator -ArgumentList @(
    "-avd", $AvdName,
    "-no-snapshot",
    "-no-boot-anim",
    "-gpu", $Gpu,
    "-memory", "1536",
    "-no-metrics"
) -WorkingDirectory $root

Write-Host "Started $AvdName with GPU mode: $Gpu"
