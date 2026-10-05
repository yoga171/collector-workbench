param(
    [string]$Serial = "emulator-5554",
    [int]$ProxyPort = 8080
)

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$keyword = Read-Host "Keyword, e.g. nike"
$pagesText = Read-Host "Pages, e.g. 5"
$excelName = Read-Host "Excel file name, e.g. nike_api.xlsx"

if ([string]::IsNullOrWhiteSpace($keyword)) { $keyword = "nike" }
if ([string]::IsNullOrWhiteSpace($pagesText)) { $pagesText = "5" }
if ([string]::IsNullOrWhiteSpace($excelName)) { $excelName = "$keyword`_api.xlsx" }

$pages = [int]$pagesText
$env:PYTHONPATH = Join-Path $root "src"
$excelPath = Join-Path (Join-Path $root "output") $excelName

& (Join-Path $root "scripts\setup_emulator_proxy.ps1") -Serial $Serial -Port $ProxyPort
try {
    & (Join-Path $root ".venv\Scripts\python.exe") -m douyin_crawler.cli run `
        --keyword $keyword `
        --pages $pages `
        --excel $excelPath `
        --serial $Serial `
        --proxy-port $ProxyPort
}
finally {
    & (Join-Path $root "scripts\clear_emulator_proxy.ps1") -Serial $Serial -Port $ProxyPort
}
