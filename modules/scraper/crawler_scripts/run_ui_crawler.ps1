param(
    [string]$Serial = "emulator-5554"
)

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$keyword = Read-Host "Keyword, e.g. nike"
$pagesText = Read-Host "Pages, e.g. 5"
$excelName = Read-Host "Excel file name, e.g. nike_ui.xlsx"

if ([string]::IsNullOrWhiteSpace($keyword)) { $keyword = "nike" }
if ([string]::IsNullOrWhiteSpace($pagesText)) { $pagesText = "5" }
if ([string]::IsNullOrWhiteSpace($excelName)) { $excelName = "$keyword`_ui.xlsx" }

$pages = [int]$pagesText
$env:PYTHONPATH = Join-Path $root "src"
$excelPath = Join-Path (Join-Path $root "output") $excelName

& (Join-Path $root ".venv\Scripts\python.exe") -m douyin_crawler.cli run-ui `
    --keyword $keyword `
    --pages $pages `
    --excel $excelPath `
    --serial $Serial
