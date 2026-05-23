[CmdletBinding()]
param(
    [string]$RepoRoot = "C:\OpenCartStockSync",

    # Modes:
    # Default = ReviewOnly
    [switch]$DryRunImport,
    [switch]$RunImport,
    [switch]$Headed,

    # Optional skips for debugging.
    [switch]$SkipOpenCartExport,
    [switch]$SkipEntersoftExport,

    # Email is enabled by default. Disable with: -SendEmail:$false
    [bool]$SendEmail = $true,

    # Profiles.
    [string]$BridgeExportProfile = "Bridge",
    [string]$StockImportProfile = "stock-only",

    # SQL/export settings.
    [string]$SqlServer = "ERPSERVER",
    [string]$Database = "TRANOULIS_NEW_db",

    # Bridge safety thresholds.
    [int]$MaxOutputRows = 10000,
    [int]$MaxDisabledNewCount = 500,
    [double]$MaxDisabledRatioPercent = 70.0,

    # Import monitor settings.
    [int]$ImportMaxWaitSec = 900,
    [int]$ImportTimeoutMs = 300000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-OrchestratorLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line

    if ($script:OrchestratorLog) {
        Add-Content -Path $script:OrchestratorLog -Value $line -Encoding UTF8
    }
}

function Invoke-ExternalStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StepName,

        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,

        [switch]$AllowFailure
    )

    Write-OrchestratorLog "STEP_START name=$StepName"
    Write-OrchestratorLog "command=$FilePath $($ArgumentList -join ' ')"

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    try {
        & $FilePath @ArgumentList 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            Add-Content -Path $script:OrchestratorLog -Value $line -Encoding UTF8
        }

        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    Write-OrchestratorLog "STEP_END name=$StepName exit_code=$exitCode"

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "Step failed: $StepName exit_code=$exitCode"
    }

    return $exitCode
}

function Read-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        throw "JSON file not found: $Path"
    }

    return Get-Content -Path $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [object]$Object
    )

    $Object | ConvertTo-Json -Depth 20 | Set-Content -Path $Path -Encoding UTF8
}

function Set-ReviewStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ReviewFile,

        [Parameter(Mandatory = $true)]
        [string]$Status,

        [Parameter(Mandatory = $true)]
        [bool]$OkToUpload,

        [hashtable]$OrchestratorData = @{}
    )

    if (-not (Test-Path $ReviewFile)) {
        Write-OrchestratorLog "WARNING review file not found for update: $ReviewFile"
        return
    }

    $review = Read-JsonFile -Path $ReviewFile

    $review.status = $Status
    $review.ok_to_upload = $OkToUpload

    $payload = [ordered]@{
        mode = $script:Mode
        updated_at = (Get-Date).ToString("s")
        orchestrator_log = $script:OrchestratorLog
    }

    foreach ($key in $OrchestratorData.Keys) {
        $payload[$key] = $OrchestratorData[$key]
    }

    $review | Add-Member -Force -NotePropertyName "orchestrator" -NotePropertyValue $payload

    Write-JsonFile -Path $ReviewFile -Object $review
}

function Copy-ReviewToLatest {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ActualReviewFile,

        [Parameter(Mandatory = $true)]
        [string]$LatestReviewFile
    )

    if ((Test-Path $ActualReviewFile) -and ($ActualReviewFile -ne $LatestReviewFile)) {
        Copy-Item -Path $ActualReviewFile -Destination $LatestReviewFile -Force
    }
}

function Send-StockSyncEmail {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Status
    )

    if (-not $SendEmail) {
        Write-OrchestratorLog "email_skipped SendEmail=false"
        return 0
    }

    if (-not (Test-Path $SendEmailScript)) {
        Write-OrchestratorLog "email_skipped script missing: $SendEmailScript"
        return 1
    }

    $emailArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $SendEmailScript,
        "-RunDir", $LatestDir,
        "-Status", $Status
    )

    return Invoke-ExternalStep `
        -StepName "send_email_$Status" `
        -FilePath "powershell.exe" `
        -ArgumentList $emailArgs `
        -AllowFailure
}

function Assert-RequiredFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path $Path)) {
        throw "$Label not found: $Path"
    }
}

# -----------------------------
# Init
# -----------------------------

if ($DryRunImport -and $RunImport) {
    throw "Choose only one mode: -DryRunImport or -RunImport, not both."
}

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)

if (-not (Test-Path $RepoRoot)) {
    throw "RepoRoot does not exist: $RepoRoot"
}

$script:Mode = "ReviewOnly"

if ($DryRunImport) {
    $script:Mode = "DryRunImport"
}

if ($RunImport) {
    $script:Mode = "RunImport"
}

$ToolsDir = Join-Path $RepoRoot "tools"
$LogsDir = Join-Path $RepoRoot "logs"
$RunsDir = Join-Path $RepoRoot "runs"
$LatestDir = Join-Path $RunsDir "latest"

$OpenCartExportScript = Join-Path $ToolsDir "run_opencart_bridge_export.ps1"
$EntersoftExportScript = Join-Path $ToolsDir "export_entersoft_stock.ps1"
$BridgeRunnerScript = Join-Path $ToolsDir "run_stock_bridge.ps1"
$StockImportScript = Join-Path $ToolsDir "opencart_import_stock_csv_playwright.py"
$SendEmailScript = Join-Path $ToolsDir "send_stock_sync_email.ps1"

$PythonExe = Join-Path $RepoRoot "runtime\python\python.exe"
$PlaywrightBrowserPath = Join-Path $RepoRoot "runtime\ms-playwright"

$OpenCartExportCsv = Join-Path $RepoRoot "input\opencart_export.csv"
$EntersoftStockCsv = Join-Path $RepoRoot "exports\entersoft_stock.ready.csv"
$LatestOcStockCsv = Join-Path $LatestDir "oc_stock.csv"
$LatestReviewJson = Join-Path $LatestDir "review.json"

New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
New-Item -ItemType Directory -Path $RunsDir -Force | Out-Null

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$script:OrchestratorLog = Join-Path $LogsDir "full_stock_sync_${script:Mode}_$Stamp.log"
New-Item -ItemType File -Path $script:OrchestratorLog -Force | Out-Null

$StartedAt = Get-Date

Write-OrchestratorLog "Starting full stock sync"
Write-OrchestratorLog "mode=$script:Mode"
Write-OrchestratorLog "repo_root=$RepoRoot"
Write-OrchestratorLog "headed=$Headed"
Write-OrchestratorLog "send_email=$SendEmail"
Write-OrchestratorLog "skip_opencart_export=$SkipOpenCartExport"
Write-OrchestratorLog "skip_entersoft_export=$SkipEntersoftExport"
Write-OrchestratorLog "bridge_export_profile=$BridgeExportProfile"
Write-OrchestratorLog "stock_import_profile=$StockImportProfile"
Write-OrchestratorLog "orchestrator_log=$script:OrchestratorLog"

$FinalStatus = "UNKNOWN"
$FinalExitCode = 0
$ActualRunDir = ""
$ActualReviewFile = ""

try {
    # Required scripts.
    Assert-RequiredFile -Path $BridgeRunnerScript -Label "Bridge runner"
    Assert-RequiredFile -Path $SendEmailScript -Label "Email script"

    if (-not $SkipOpenCartExport) {
        Assert-RequiredFile -Path $OpenCartExportScript -Label "OpenCart Bridge export script"
    }

    if (-not $SkipEntersoftExport) {
        Assert-RequiredFile -Path $EntersoftExportScript -Label "Entersoft export script"
    }

    if ($DryRunImport -or $RunImport) {
        Assert-RequiredFile -Path $PythonExe -Label "Portable Python"
        Assert-RequiredFile -Path $StockImportScript -Label "OpenCart stock import script"
    }

    # Portable runtime env.
    $env:PYTHONPATH = $RepoRoot
    $env:PLAYWRIGHT_BROWSERS_PATH = $PlaywrightBrowserPath

    Write-OrchestratorLog "PYTHONPATH=$env:PYTHONPATH"
    Write-OrchestratorLog "PLAYWRIGHT_BROWSERS_PATH=$env:PLAYWRIGHT_BROWSERS_PATH"

    # -----------------------------
    # 1. Export OpenCart Bridge profile
    # -----------------------------
    if ($SkipOpenCartExport) {
        Write-OrchestratorLog "STEP_SKIPPED name=opencart_bridge_export"
        Assert-RequiredFile -Path $OpenCartExportCsv -Label "Existing OpenCart export CSV"
    } else {
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $OpenCartExportScript,
            "-RepoRoot", $RepoRoot,
            "-ProfileName", $BridgeExportProfile,
            "-OutputFile", $OpenCartExportCsv
        )

        if ($Headed) {
            $args += "-Headed"
        }

        Invoke-ExternalStep `
            -StepName "opencart_bridge_export" `
            -FilePath "powershell.exe" `
            -ArgumentList $args | Out-Null
    }

    Assert-RequiredFile -Path $OpenCartExportCsv -Label "OpenCart export CSV"
    Write-OrchestratorLog "opencart_export_csv=$OpenCartExportCsv"

    # -----------------------------
    # 2. Export Entersoft stock
    # -----------------------------
    if ($SkipEntersoftExport) {
        Write-OrchestratorLog "STEP_SKIPPED name=entersoft_stock_export"
        Assert-RequiredFile -Path $EntersoftStockCsv -Label "Existing Entersoft stock CSV"
    } else {
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $EntersoftExportScript,
            "-RepoRoot", $RepoRoot,
            "-SqlServer", $SqlServer,
            "-Database", $Database
        )

        Invoke-ExternalStep `
            -StepName "entersoft_stock_export" `
            -FilePath "powershell.exe" `
            -ArgumentList $args | Out-Null
    }

    Assert-RequiredFile -Path $EntersoftStockCsv -Label "Entersoft stock CSV"
    Write-OrchestratorLog "entersoft_stock_csv=$EntersoftStockCsv"

    # -----------------------------
    # 3. Bridge
    # -----------------------------
    $bridgeArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $BridgeRunnerScript,
        "-RepoRoot", $RepoRoot,
        "-StockCsv", $EntersoftStockCsv,
        "-OpenCartCsv", $OpenCartExportCsv,
        "-MaxOutputRows", "$MaxOutputRows",
        "-MaxDisabledNewCount", "$MaxDisabledNewCount",
        "-MaxDisabledRatioPercent", "$MaxDisabledRatioPercent"
    )

    $bridgeExitCode = Invoke-ExternalStep `
        -StepName "stock_bridge" `
        -FilePath "powershell.exe" `
        -ArgumentList $bridgeArgs `
        -AllowFailure

    if (-not (Test-Path $LatestReviewJson)) {
        throw "Bridge did not produce latest review.json: $LatestReviewJson"
    }

    $review = Read-JsonFile -Path $LatestReviewJson
    $ActualRunDir = [string]$review.run_dir
    $ActualReviewFile = [string]$review.outputs.review_json

    Write-OrchestratorLog "bridge_review_status=$($review.status)"
    Write-OrchestratorLog "bridge_ok_to_upload=$($review.ok_to_upload)"
    Write-OrchestratorLog "actual_run_dir=$ActualRunDir"
    Write-OrchestratorLog "actual_review_file=$ActualReviewFile"

    if ($bridgeExitCode -ne 0) {
        $FinalStatus = "BLOCKED"
        Set-ReviewStatus `
            -ReviewFile $ActualReviewFile `
            -Status $FinalStatus `
            -OkToUpload $false `
            -OrchestratorData @{
                bridge_exit_code = $bridgeExitCode
                finished_at = (Get-Date).ToString("s")
            }

        Copy-ReviewToLatest -ActualReviewFile $ActualReviewFile -LatestReviewFile $LatestReviewJson
        Send-StockSyncEmail -Status $FinalStatus | Out-Null
        exit 2
    }

    if (-not [bool]$review.ok_to_upload) {
        $FinalStatus = [string]$review.status

        if ([string]::IsNullOrWhiteSpace($FinalStatus)) {
            $FinalStatus = "BLOCKED"
        }

        Set-ReviewStatus `
            -ReviewFile $ActualReviewFile `
            -Status $FinalStatus `
            -OkToUpload $false `
            -OrchestratorData @{
                reason = "bridge_not_ok_to_upload"
                finished_at = (Get-Date).ToString("s")
            }

        Copy-ReviewToLatest -ActualReviewFile $ActualReviewFile -LatestReviewFile $LatestReviewJson
        Send-StockSyncEmail -Status $FinalStatus | Out-Null

        Write-OrchestratorLog "Stopping because bridge status is not uploadable: $FinalStatus"
        exit 0
    }

    Assert-RequiredFile -Path $LatestOcStockCsv -Label "Latest oc_stock.csv"

    # -----------------------------
    # 4. ReviewOnly mode
    # -----------------------------
    if (-not $DryRunImport -and -not $RunImport) {
        $FinalStatus = "READY"

        Set-ReviewStatus `
            -ReviewFile $ActualReviewFile `
            -Status $FinalStatus `
            -OkToUpload $true `
            -OrchestratorData @{
                mode_result = "review_only_no_import_attempted"
                finished_at = (Get-Date).ToString("s")
            }

        Copy-ReviewToLatest -ActualReviewFile $ActualReviewFile -LatestReviewFile $LatestReviewJson
        Send-StockSyncEmail -Status $FinalStatus | Out-Null

        Write-OrchestratorLog "ReviewOnly complete"
        $FinalExitCode = 0
        return
    }

    # -----------------------------
    # 5. Import mode: dry-run or real
    # -----------------------------
    $ImportModeLabel = if ($DryRunImport) { "DRY_RUN" } else { "REAL_IMPORT" }
    $ImportReport = Join-Path $ActualRunDir "opencart_import_${ImportModeLabel}.json"
    $ImportLog = Join-Path $ActualRunDir "opencart_import_${ImportModeLabel}.log"

    $importArgs = @(
        $StockImportScript,
        "--repo-root", $RepoRoot,
        "--csv-file", $LatestOcStockCsv,
        "--profile", $StockImportProfile,
        "--report-file", $ImportReport,
        "--log-file", $ImportLog,
        "--timeout-ms", "$ImportTimeoutMs",
        "--max-wait-sec", "$ImportMaxWaitSec"
    )

    if ($DryRunImport) {
        $importArgs += "--dry-run"
    }

    if ($Headed) {
        $importArgs += "--headed"
    } else {
        $importArgs += "--headless"
    }

    $importExitCode = Invoke-ExternalStep `
        -StepName "opencart_stock_import_$ImportModeLabel" `
        -FilePath $PythonExe `
        -ArgumentList $importArgs `
        -AllowFailure

    $importData = @{
        mode = $ImportModeLabel
        exit_code = $importExitCode
        report_file = $ImportReport
        log_file = $ImportLog
    }

    if (Test-Path $ImportReport) {
        try {
            $parsedImportReport = Read-JsonFile -Path $ImportReport
            $importData["report_ok"] = [bool]$parsedImportReport.ok
            $importData["dry_run"] = [bool]$parsedImportReport.dry_run
        } catch {
            $importData["report_parse_error"] = [string]$_
        }
    }

    if ($importExitCode -ne 0) {
        $FinalStatus = if ($DryRunImport) { "DRY_RUN_FAILED" } else { "IMPORT_FAILED" }

        Set-ReviewStatus `
            -ReviewFile $ActualReviewFile `
            -Status $FinalStatus `
            -OkToUpload $false `
            -OrchestratorData @{
                opencart_import = $importData
                finished_at = (Get-Date).ToString("s")
            }

        Copy-ReviewToLatest -ActualReviewFile $ActualReviewFile -LatestReviewFile $LatestReviewJson
        Send-StockSyncEmail -Status $FinalStatus | Out-Null

        Write-OrchestratorLog "Import step failed: $FinalStatus"
        exit 3
    }

    $FinalStatus = if ($DryRunImport) { "DRY_RUN_OK" } else { "IMPORT_OK" }

    Set-ReviewStatus `
        -ReviewFile $ActualReviewFile `
        -Status $FinalStatus `
        -OkToUpload $true `
        -OrchestratorData @{
            opencart_import = $importData
            finished_at = (Get-Date).ToString("s")
        }

    Copy-ReviewToLatest -ActualReviewFile $ActualReviewFile -LatestReviewFile $LatestReviewJson
    Send-StockSyncEmail -Status $FinalStatus | Out-Null

    Write-OrchestratorLog "Full stock sync complete status=$FinalStatus"
    $FinalExitCode = 0
}
catch {
    $FinalStatus = "ORCHESTRATOR_FAILED"
    $FinalExitCode = 9

    Write-OrchestratorLog "ERROR: $($_.Exception.Message)"

    if ($ActualReviewFile -and (Test-Path $ActualReviewFile)) {
        Set-ReviewStatus `
            -ReviewFile $ActualReviewFile `
            -Status $FinalStatus `
            -OkToUpload $false `
            -OrchestratorData @{
                error = $_.Exception.Message
                finished_at = (Get-Date).ToString("s")
            }

        if (Test-Path $LatestReviewJson) {
            Copy-ReviewToLatest -ActualReviewFile $ActualReviewFile -LatestReviewFile $LatestReviewJson
        }
    }

    try {
        if (Test-Path $LatestDir) {
            Send-StockSyncEmail -Status $FinalStatus | Out-Null
        }
    } catch {
        Write-OrchestratorLog "ERROR sending failure email: $($_.Exception.Message)"
    }

    throw
}
finally {
    $FinishedAt = Get-Date
    $DurationSec = [Math]::Round(($FinishedAt - $StartedAt).TotalSeconds, 2)

    Write-OrchestratorLog "finished_status=$FinalStatus"
    Write-OrchestratorLog "duration_sec=$DurationSec"
    Write-OrchestratorLog "log_file=$script:OrchestratorLog"

    Write-Host ""
    Write-Host "Full stock sync result"
    Write-Host "----------------------"
    Write-Host "Mode:       $script:Mode"
    Write-Host "Status:     $FinalStatus"
    Write-Host "Duration:   $DurationSec sec"
    Write-Host "Log:        $script:OrchestratorLog"
    if ($ActualRunDir) {
        Write-Host "Run dir:    $ActualRunDir"
    }
    Write-Host ""
}

exit $FinalExitCode