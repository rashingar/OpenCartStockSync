[CmdletBinding()]
param(
    [string]$RepoRoot = "C:\OpenCartStockSync",
    [string]$SqlServer = "ERPSERVER",
    [string]$Database = "TRANOULIS_NEW_db"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-RunLog {
    param([Parameter(Mandatory = $true)][string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $script:LogFile -Value $line -Encoding UTF8
}

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

$SqlFile = Join-Path $RepoRoot "sql\WH1_stock.sql"
$ExportsDir = Join-Path $RepoRoot "exports"
$LogsDir = Join-Path $RepoRoot "logs"

$TmpRawFile = Join-Path $ExportsDir "entersoft_stock.raw.tmp"
$TmpCsvFile = Join-Path $ExportsDir "entersoft_stock.tmp.csv"
$ReadyFile = Join-Path $ExportsDir "entersoft_stock.ready.csv"
$PreviousGoodFile = Join-Path $ExportsDir "previous_good_entersoft_stock.csv"

New-Item -ItemType Directory -Path $ExportsDir -Force | Out-Null
New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$script:LogFile = Join-Path $LogsDir "export_entersoft_stock_$Stamp.log"
New-Item -ItemType File -Path $script:LogFile -Force | Out-Null

Write-RunLog "Starting Entersoft stock export"
Write-RunLog "repo_root=$RepoRoot"
Write-RunLog "sql_server=$SqlServer"
Write-RunLog "database=$Database"
Write-RunLog "sql_file=$SqlFile"
Write-RunLog "ready_file=$ReadyFile"

if (-not (Test-Path $SqlFile)) {
    throw "SQL file not found: $SqlFile"
}

$sqlcmd = Get-Command sqlcmd -ErrorAction SilentlyContinue
if ($null -eq $sqlcmd) {
    throw "sqlcmd was not found. Install SQL Server command-line tools or run this on ERPSERVER where sqlcmd exists."
}

if (Test-Path $TmpRawFile) { Remove-Item $TmpRawFile -Force }
if (Test-Path $TmpCsvFile) { Remove-Item $TmpCsvFile -Force }

Write-RunLog "Running sqlcmd"

& sqlcmd `
    -S $SqlServer `
    -d $Database `
    -E `
    -i $SqlFile `
    -o $TmpRawFile `
    -h -1 `
    -W `
    -w 65535

$SqlExitCode = $LASTEXITCODE
Write-RunLog "sqlcmd_exit_code=$SqlExitCode"

if ($SqlExitCode -ne 0) {
    throw "sqlcmd failed with exit code $SqlExitCode"
}

Write-RunLog "Cleaning sqlcmd output"

$Lines = Get-Content $TmpRawFile -Encoding UTF8 |
    ForEach-Object { $_.Trim() } |
    Where-Object {
        $_ -ne "" `
        -and $_ -notmatch "rows affected" `
        -and $_ -notmatch "^-+$" `
        -and $_ -notmatch "^model\s+quantity$" `
        -and $_ -notmatch "^model,quantity$"
    } |
    ForEach-Object {
        if ($_ -match "^([0-9]{6})\s+([0-9]+)$") {
            "$($Matches[1]),$($Matches[2])"
        }
        elseif ($_ -match "^([0-9]{6}),([0-9]+)$") {
            "$($Matches[1]),$($Matches[2])"
        }
    } |
    Where-Object {
        $_ -match "^[0-9]{6},[0-9]+$"
    }

$RowCount = @($Lines).Count
Write-RunLog "data_rows=$RowCount"

if ($RowCount -le 0) {
    throw "Export produced zero valid stock rows. Refusing to overwrite ready file."
}

# Safety guard: tune later once we know normal row counts.
if ($RowCount -lt 100) {
    throw "Export produced suspiciously few rows: $RowCount. Refusing to overwrite ready file."
}

"model,quantity" | Set-Content -Path $TmpCsvFile -Encoding UTF8
$Lines | Add-Content -Path $TmpCsvFile -Encoding UTF8

Write-RunLog "Validating CSV"

$Imported = Import-Csv $TmpCsvFile
$ImportedCount = @($Imported).Count

if ($ImportedCount -ne $RowCount) {
    throw "CSV validation count mismatch. Lines=$RowCount Imported=$ImportedCount"
}

$DuplicateModels = $Imported |
    Group-Object model |
    Where-Object { $_.Count -gt 1 } |
    Select-Object -First 10

if ($DuplicateModels) {
    $Sample = ($DuplicateModels | ForEach-Object { $_.Name }) -join ", "
    throw "Duplicate models found in stock export. Sample: $Sample"
}

$InvalidRows = $Imported | Where-Object {
    $_.model -notmatch "^[0-9]{6}$" -or $_.quantity -notmatch "^[0-9]+$"
} | Select-Object -First 10

if ($InvalidRows) {
    throw "Invalid rows found in stock export. Refusing to promote."
}

if (Test-Path $ReadyFile) {
    Copy-Item $ReadyFile $PreviousGoodFile -Force
    Write-RunLog "previous_good_updated=$PreviousGoodFile"
}

Move-Item $TmpCsvFile $ReadyFile -Force

Write-RunLog "ready_file_written=$ReadyFile"
Write-RunLog "ready_rows=$ImportedCount"
Write-RunLog "Done"

Write-Host ""
Write-Host "Entersoft stock export completed"
Write-Host "-------------------------------"
Write-Host "Ready file: $ReadyFile"
Write-Host "Rows:       $ImportedCount"
Write-Host "Log:        $script:LogFile"
Write-Host ""

exit 0