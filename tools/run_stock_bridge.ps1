[CmdletBinding()]
param(
    [string]$RepoRoot = "C:\OpenCartStockSync",

    [string]$StockCsv = "",
    [string]$OpenCartCsv = "",

    [string]$RunsDir = "",
    [string]$PythonExe = "",

    [int]$MaxOutputRows = 10000,
    [int]$MaxDisabledNewCount = 500,
    [double]$MaxDisabledRatioPercent = 70.0,

    [switch]$AllowEmptyOutput
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-RunnerLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line

    if ($script:RunnerLog) {
        Add-Content -Path $script:RunnerLog -Value $line -Encoding UTF8
    }
}

function ConvertTo-Percent {
    param(
        [int]$Part,
        [int]$Total
    )

    if ($Total -le 0) {
        return 0.0
    }

    return [Math]::Round(($Part / $Total) * 100.0, 2)
}

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

if (-not (Test-Path $RepoRoot)) {
    throw "RepoRoot does not exist: $RepoRoot"
}

if ([string]::IsNullOrWhiteSpace($StockCsv)) {
    $StockCsv = Join-Path $RepoRoot "exports\entersoft_stock.ready.csv"
}

if ([string]::IsNullOrWhiteSpace($OpenCartCsv)) {
    $OpenCartCsv = Join-Path $RepoRoot "input\opencart_export.csv"
}

if ([string]::IsNullOrWhiteSpace($RunsDir)) {
    $RunsDir = Join-Path $RepoRoot "runs"
}

if ([string]::IsNullOrWhiteSpace($PythonExe)) {
    $PythonExe = Join-Path $RepoRoot "runtime\python\python.exe"
}

$BridgeScript = Join-Path $RepoRoot "tools\bridge_core.py"

if (-not (Test-Path $PythonExe)) {
    throw "Portable Python not found: $PythonExe"
}

if (-not (Test-Path $BridgeScript)) {
    throw "Bridge script not found: $BridgeScript"
}

if (-not (Test-Path $StockCsv)) {
    throw "Stock CSV not found: $StockCsv"
}

if (-not (Test-Path $OpenCartCsv)) {
    throw "OpenCart export CSV not found: $OpenCartCsv"
}

New-Item -ItemType Directory -Path $RunsDir -Force | Out-Null

$RunId = Get-Date -Format "yyyy-MM-dd_HHmmss"
$RunDir = Join-Path $RunsDir $RunId
New-Item -ItemType Directory -Path $RunDir -Force | Out-Null

$script:RunnerLog = Join-Path $RunDir "run_stock_bridge.log"
$BridgeStdoutFile = Join-Path $RunDir "bridge.stdout.json"
$BridgeStderrFile = Join-Path $RunDir "bridge.stderr.log"
$ReviewFile = Join-Path $RunDir "review.json"

New-Item -ItemType File -Path $script:RunnerLog -Force | Out-Null

Write-RunnerLog "Starting stock bridge runner"
Write-RunnerLog "repo_root=$RepoRoot"
Write-RunnerLog "run_id=$RunId"
Write-RunnerLog "run_dir=$RunDir"
Write-RunnerLog "stock_csv=$StockCsv"
Write-RunnerLog "opencart_csv=$OpenCartCsv"
Write-RunnerLog "python_exe=$PythonExe"
Write-RunnerLog "bridge_script=$BridgeScript"

$env:PYTHONPATH = $RepoRoot

$BridgeArgs = @(
    $BridgeScript,
    "--stock-csv", $StockCsv,
    "--opencart-csv", $OpenCartCsv,
    "--output-dir", $RunDir
)

Write-RunnerLog "Running bridge_core.py"

& $PythonExe @BridgeArgs 1> $BridgeStdoutFile 2> $BridgeStderrFile
$BridgeExitCode = $LASTEXITCODE

Write-RunnerLog "bridge_exit_code=$BridgeExitCode"

$BridgeStdout = ""
$BridgeStderr = ""

if (Test-Path $BridgeStdoutFile) {
    $BridgeStdout = Get-Content -Path $BridgeStdoutFile -Raw -Encoding UTF8
}

if (Test-Path $BridgeStderrFile) {
    $BridgeStderr = Get-Content -Path $BridgeStderrFile -Raw -Encoding UTF8
}

$BridgeSummary = $null

if (-not [string]::IsNullOrWhiteSpace($BridgeStdout)) {
    try {
        $BridgeSummary = $BridgeStdout | ConvertFrom-Json
    } catch {
        Write-RunnerLog "Could not parse bridge stdout as JSON"
    }
}

$HardFailures = New-Object System.Collections.Generic.List[string]
$Warnings = New-Object System.Collections.Generic.List[string]

if ($BridgeExitCode -ne 0) {
    $HardFailures.Add("bridge_core.py exited with code $BridgeExitCode")
}

if ($null -eq $BridgeSummary) {
    $HardFailures.Add("Bridge summary JSON missing or invalid")
}

if (-not [string]::IsNullOrWhiteSpace($BridgeStderr)) {
    $Warnings.Add("bridge stderr is not empty; check bridge.stderr.log")
}

$OutputRows = 0
$DisabledNewCount = 0
$DisabledRatioPercent = 0.0
$StatusWouldChangeCount = 0
$QuantityChangedCount = 0
$PriceZeroForcedDisabledCount = 0
$PriceZeroForcedDisabledProducts = @()

if ($null -ne $BridgeSummary) {
    $OutputRows = [int]$BridgeSummary.output_rows
    $DisabledNewCount = [int]$BridgeSummary.disabled_new_count
    $StatusWouldChangeCount = [int]$BridgeSummary.status_would_change_count
    $QuantityChangedCount = [int]$BridgeSummary.quantity_changed_count

    if ($BridgeSummary.PSObject.Properties.Name -contains "price_zero_forced_disabled_count") {
        $PriceZeroForcedDisabledCount = [int]$BridgeSummary.price_zero_forced_disabled_count
    }

    if ($BridgeSummary.PSObject.Properties.Name -contains "price_zero_forced_disabled_products") {
        $PriceZeroForcedDisabledProducts = @($BridgeSummary.price_zero_forced_disabled_products)
    }

    $DisabledRatioPercent = ConvertTo-Percent -Part $DisabledNewCount -Total $OutputRows

    if ($OutputRows -eq 0 -and -not $AllowEmptyOutput) {
        $Warnings.Add("No stock changes detected; oc_stock.csv has 0 data rows")
    }

    if ($OutputRows -gt $MaxOutputRows) {
        $HardFailures.Add("Too many output rows: $OutputRows > $MaxOutputRows")
    }

    if ($DisabledNewCount -gt $MaxDisabledNewCount) {
        $HardFailures.Add("Too many products would become disabled: $DisabledNewCount > $MaxDisabledNewCount")
    }

    if ($DisabledRatioPercent -gt $MaxDisabledRatioPercent) {
        $HardFailures.Add("Disabled ratio too high: $DisabledRatioPercent% > $MaxDisabledRatioPercent%")
    }

    if ([int]$BridgeSummary.ignored_stock_rows_count -gt 0) {
        $Warnings.Add("Ignored stock rows: $($BridgeSummary.ignored_stock_rows_count)")
    }

    if ([int]$BridgeSummary.ignored_opencart_rows_count -gt 0) {
        $Warnings.Add("Ignored OpenCart rows: $($BridgeSummary.ignored_opencart_rows_count)")
    }

    if ([int]$BridgeSummary.stock_not_in_opencart_count -gt 0) {
        $Warnings.Add("Stock models not in OpenCart: $($BridgeSummary.stock_not_in_opencart_count)")
    }

    if ([int]$BridgeSummary.opencart_missing_in_stock_count -gt 0) {
        $Warnings.Add("OpenCart models missing in stock: $($BridgeSummary.opencart_missing_in_stock_count)")
    }
}

$Status = "READY"
$OkToUpload = $true

if ($HardFailures.Count -gt 0) {
    $Status = "BLOCKED"
    $OkToUpload = $false
} elseif ($OutputRows -eq 0 -and -not $AllowEmptyOutput) {
    $Status = "NO_CHANGES"
    $OkToUpload = $false
}

$Review = [ordered]@{
    ok_to_upload = $OkToUpload
    status = $Status
    run_id = $RunId
    run_dir = $RunDir
    created_at = (Get-Date).ToString("s")

    inputs = [ordered]@{
        stock_csv = $StockCsv
        opencart_csv = $OpenCartCsv
    }

    outputs = [ordered]@{
        oc_stock_csv = if ($null -ne $BridgeSummary) { [string]$BridgeSummary.oc_stock_csv } else { "" }
        summary_csv = if ($null -ne $BridgeSummary) { [string]$BridgeSummary.summary_csv } else { "" }
        bridge_log = if ($null -ne $BridgeSummary) { [string]$BridgeSummary.bridge_log } else { "" }
        review_json = $ReviewFile
        runner_log = $script:RunnerLog
        bridge_stdout = $BridgeStdoutFile
        bridge_stderr = $BridgeStderrFile
    }

    counts = [ordered]@{
        output_rows = $OutputRows
        quantity_changed_count = $QuantityChangedCount
        status_would_change_count = $StatusWouldChangeCount
        disabled_new_count = $DisabledNewCount
        disabled_ratio_percent = $DisabledRatioPercent
        price_zero_forced_disabled_count = $PriceZeroForcedDisabledCount
        enabled_new_count = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.enabled_new_count } else { 0 }
        stock_rows = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.stock_rows } else { 0 }
        opencart_rows = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.opencart_rows } else { 0 }
        ignored_stock_rows_count = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.ignored_stock_rows_count } else { 0 }
        ignored_opencart_rows_count = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.ignored_opencart_rows_count } else { 0 }
        opencart_missing_in_stock_count = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.opencart_missing_in_stock_count } else { 0 }
        stock_not_in_opencart_count = if ($null -ne $BridgeSummary) { [int]$BridgeSummary.stock_not_in_opencart_count } else { 0 }
    }

    price_zero_forced_disabled_products = @($PriceZeroForcedDisabledProducts)

    thresholds = [ordered]@{
        max_output_rows = $MaxOutputRows
        max_disabled_new_count = $MaxDisabledNewCount
        max_disabled_ratio_percent = $MaxDisabledRatioPercent
        allow_empty_output = [bool]$AllowEmptyOutput
    }

    safety = [ordered]@{
        hard_failures = @($HardFailures)
        warnings = @($Warnings)
    }
}

$Review | ConvertTo-Json -Depth 12 | Set-Content -Path $ReviewFile -Encoding UTF8

Write-RunnerLog "review_written=$ReviewFile"
Write-RunnerLog "status=$Status"
Write-RunnerLog "ok_to_upload=$OkToUpload"
Write-RunnerLog "output_rows=$OutputRows"
Write-RunnerLog "disabled_new_count=$DisabledNewCount"
Write-RunnerLog "disabled_ratio_percent=$DisabledRatioPercent"
Write-RunnerLog "price_zero_forced_disabled_count=$PriceZeroForcedDisabledCount"

if ($OkToUpload) {
    $LatestDir = Join-Path $RunsDir "latest"

    if (Test-Path $LatestDir) {
        Remove-Item -Path $LatestDir -Recurse -Force
    }

    New-Item -ItemType Directory -Path $LatestDir -Force | Out-Null

    Copy-Item -Path (Join-Path $RunDir "oc_stock.csv") -Destination (Join-Path $LatestDir "oc_stock.csv") -Force
    Copy-Item -Path (Join-Path $RunDir "summary.csv") -Destination (Join-Path $LatestDir "summary.csv") -Force
    Copy-Item -Path (Join-Path $RunDir "bridge.log") -Destination (Join-Path $LatestDir "bridge.log") -Force
    Copy-Item -Path $ReviewFile -Destination (Join-Path $LatestDir "review.json") -Force

    Write-RunnerLog "latest_updated=$LatestDir"
} else {
    Write-RunnerLog "latest_not_updated_because_status=$Status"
}

Write-Host ""
Write-Host "Stock bridge result"
Write-Host "-------------------"
Write-Host "Status:             $Status"
Write-Host "OK to upload:       $OkToUpload"
Write-Host "Run dir:            $RunDir"
Write-Host "Output rows:        $OutputRows"
Write-Host "Disabled new count: $DisabledNewCount"
Write-Host "Price zero forced:  $PriceZeroForcedDisabledCount"
Write-Host "Review:             $ReviewFile"
Write-Host ""

if ($HardFailures.Count -gt 0) {
    Write-Host "Hard failures:"
    foreach ($failure in $HardFailures) {
        Write-Host "- $failure"
    }
    exit 2
}

exit 0