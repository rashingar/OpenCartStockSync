[CmdletBinding()]
param(
    [string]$RepoRoot = "C:\OpenCartStockSync",
    [string]$ProfileName = "Bridge",
    [string]$OutputFile = "C:\OpenCartStockSync\input\opencart_export.csv",
    [switch]$Headed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$PythonExe = Join-Path $RepoRoot "runtime\python\python.exe"
$Script = Join-Path $RepoRoot "tools\opencart_export_bridge_profile_playwright.py"
$BrowserPath = Join-Path $RepoRoot "runtime\ms-playwright"

if (-not (Test-Path $PythonExe)) {
    throw "Portable Python not found: $PythonExe"
}

if (-not (Test-Path $Script)) {
    throw "Export script not found: $Script"
}

$env:PYTHONPATH = $RepoRoot
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserPath

$Args = @(
    $Script,
    "--repo-root", $RepoRoot,
    "--profile", $ProfileName,
    "--export-route", "extension/ka_extensions/csv_product_export/ka_product_export",
    "--output-file", $OutputFile
)

if ($Headed) {
    $Args += "--headed"
} else {
    $Args += "--headless"
}

& $PythonExe @Args
exit $LASTEXITCODE