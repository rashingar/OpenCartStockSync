[CmdletBinding()]
param(
    [string]$RepoRoot = "C:\OpenCartStockSync",
    [string]$RunDir = "C:\OpenCartStockSync\runs\latest",
    [string]$SubjectPrefix = "[OpenCartStockSync]",
    [string]$Status = "REPORT",
    [int]$MaxSummaryRowsInBody = 200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-EnvFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        throw "Env file not found: $Path"
    }

    $values = @{}

    Get-Content $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()

        if ([string]::IsNullOrWhiteSpace($line)) { return }
        if ($line.StartsWith("#")) { return }
        if ($line -notmatch "=") { return }

        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()

        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        if (-not [string]::IsNullOrWhiteSpace($key)) {
            $values[$key] = $value
        }
    }

    return $values
}

function Require-EnvValue {
    param(
        [hashtable]$Env,
        [string]$Key
    )

    if (-not $Env.ContainsKey($Key) -or [string]::IsNullOrWhiteSpace([string]$Env[$Key])) {
        throw "Missing required email config value: $Key"
    }

    return [string]$Env[$Key]
}

function Get-OptionalFile {
    param([string]$Path)

    if (Test-Path $Path) {
        return (Resolve-Path $Path).Path
    }

    return $null
}

function HtmlEncode {
    param([object]$Value)

    return [System.Net.WebUtility]::HtmlEncode([string]$Value)
}

function Get-JsonProp {
    param(
        [object]$Object,
        [string]$Name,
        [object]$Default = ""
    )

    if ($null -eq $Object) {
        return $Default
    }

    if ($Object.PSObject.Properties.Name -contains $Name) {
        return $Object.$Name
    }

    return $Default
}

function Convert-SummaryCsvToHtmlTable {
    param(
        [string]$SummaryPath,
        [int]$MaxRows = 200
    )

    if (-not $SummaryPath -or -not (Test-Path $SummaryPath)) {
        return "<p><em>summary.csv was not found.</em></p>"
    }

    $rows = Import-Csv -Path $SummaryPath -Encoding UTF8

    if (-not $rows -or @($rows).Count -eq 0) {
        return "<p><em>summary.csv has no data rows.</em></p>"
    }

    $totalRows = @($rows).Count
    $displayRows = @($rows | Select-Object -First $MaxRows)

    $html = New-Object System.Text.StringBuilder

    [void]$html.AppendLine("<h2 style='font-size:17px; margin-top:22px; margin-bottom:8px;'>Summary changes</h2>")
    [void]$html.AppendLine("<p style='margin:0 0 8px 0;'>Showing $($displayRows.Count) of $totalRows rows. Full file is attached as <strong>summary.csv</strong>.</p>")

    [void]$html.AppendLine("<table cellpadding='3' cellspacing='0' border='1' style='border-collapse:collapse; font-family:Arial, sans-serif; font-size:12px; line-height:1.2; width:100%;'>")
    [void]$html.AppendLine("<thead>")
    [void]$html.AppendLine("<tr style='background:#f2f2f2;'>")
    [void]$html.AppendLine("<th align='left' style='padding:3px 6px;'>model</th>")
    [void]$html.AppendLine("<th align='left' style='padding:3px 6px;'>name</th>")
    [void]$html.AppendLine("<th align='right' style='padding:3px 6px;'>old_qty</th>")
    [void]$html.AppendLine("<th align='right' style='padding:3px 6px;'>new_qty</th>")
    [void]$html.AppendLine("<th align='right' style='padding:3px 6px;'>old_status</th>")
    [void]$html.AppendLine("<th align='right' style='padding:3px 6px;'>new_status</th>")
    [void]$html.AppendLine("</tr>")
    [void]$html.AppendLine("</thead>")
    [void]$html.AppendLine("<tbody>")

    foreach ($row in $displayRows) {
        $oldQty = [int]$row.old_qty
        $newQty = [int]$row.new_qty
        $oldStatus = [int]$row.old_status
        $newStatus = [int]$row.new_status

        $rowStyle = ""

        if ($newQty -eq 0 -or $newStatus -eq 0) {
            $rowStyle = "background:#fff3f3;"
        } elseif ($newQty -gt $oldQty) {
            $rowStyle = "background:#f3fff4;"
        } elseif ($newQty -lt $oldQty) {
            $rowStyle = "background:#fffaf0;"
        }

        [void]$html.AppendLine("<tr style='$rowStyle'>")
        [void]$html.AppendLine("<td style='padding:3px 6px;'>$(HtmlEncode $row.model)</td>")
        [void]$html.AppendLine("<td style='padding:3px 6px;'>$(HtmlEncode $row.name)</td>")
        [void]$html.AppendLine("<td style='padding:3px 6px;' align='right'>$(HtmlEncode $row.old_qty)</td>")
        [void]$html.AppendLine("<td style='padding:3px 6px;' align='right'><strong>$(HtmlEncode $row.new_qty)</strong></td>")
        [void]$html.AppendLine("<td style='padding:3px 6px;' align='right'>$(HtmlEncode $row.old_status)</td>")
        [void]$html.AppendLine("<td style='padding:3px 6px;' align='right'><strong>$(HtmlEncode $row.new_status)</strong></td>")
        [void]$html.AppendLine("</tr>")
    }

    [void]$html.AppendLine("</tbody>")
    [void]$html.AppendLine("</table>")

    if ($totalRows -gt $MaxRows) {
        [void]$html.AppendLine("<p><strong>Note:</strong> Table truncated in email body. Full summary is attached.</p>")
    }

    return $html.ToString()
}

function Convert-PriceZeroProductsToHtmlTable {
    param(
        [object]$Review,
        [int]$MaxRows = 200
    )

    if (-not $Review) {
        return ""
    }

    if (-not ($Review.PSObject.Properties.Name -contains "price_zero_forced_disabled_products")) {
        return ""
    }

    $products = @($Review.price_zero_forced_disabled_products)

    if ($products.Count -eq 0) {
        return @"
<h2 style='font-size:17px; margin-top:18px; margin-bottom:8px;'>Price = 0 forced disabled</h2>
<p style='margin:0 0 12px 0;'>No products were forced disabled because of zero price.</p>
"@
    }

    $displayRows = @($products | Select-Object -First $MaxRows)
    $totalRows = $products.Count

    $html = New-Object System.Text.StringBuilder

    [void]$html.AppendLine("<h2 style='font-size:17px; margin-top:18px; margin-bottom:8px;'>Price = 0 forced disabled</h2>")
    [void]$html.AppendLine("<p style='margin:0 0 8px 0;'>Showing $($displayRows.Count) of $totalRows products where OpenCart price is 0, so status was forced to 0.</p>")

    [void]$html.AppendLine("<table cellpadding='3' cellspacing='0' border='1' style='border-collapse:collapse; font-family:Arial, sans-serif; font-size:12px; line-height:1.2; width:100%;'>")
    [void]$html.AppendLine("<thead>")
    [void]$html.AppendLine("<tr style='background:#f2f2f2;'>")
    [void]$html.AppendLine("<th align='left' style='padding:3px 6px;'>model</th>")
    [void]$html.AppendLine("<th align='left' style='padding:3px 6px;'>name</th>")
    [void]$html.AppendLine("</tr>")
    [void]$html.AppendLine("</thead>")
    [void]$html.AppendLine("<tbody>")

    foreach ($product in $displayRows) {
        [void]$html.AppendLine("<tr style='background:#fff3f3;'>")
        [void]$html.AppendLine("<td style='padding:3px 6px;'>$(HtmlEncode $product.model)</td>")
        [void]$html.AppendLine("<td style='padding:3px 6px;'>$(HtmlEncode $product.name)</td>")
        [void]$html.AppendLine("</tr>")
    }

    [void]$html.AppendLine("</tbody>")
    [void]$html.AppendLine("</table>")

    if ($totalRows -gt $MaxRows) {
        [void]$html.AppendLine("<p><strong>Note:</strong> Price-zero table truncated in email body.</p>")
    }

    return $html.ToString()
}

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$RunDir = [System.IO.Path]::GetFullPath($RunDir)

$EmailEnvFile = Join-Path $RepoRoot ".secrets\email.env"
$LogsDir = Join-Path $RepoRoot "logs"

New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$EmailLog = Join-Path $LogsDir "stock_sync_email_$Stamp.log"

function Write-EmailLog {
    param([string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $EmailLog -Value $line -Encoding UTF8
}

Write-EmailLog "Starting stock sync email"
Write-EmailLog "repo_root=$RepoRoot"
Write-EmailLog "run_dir=$RunDir"
Write-EmailLog "email_env=$EmailEnvFile"

$config = Read-EnvFile -Path $EmailEnvFile

$smtpHost = Require-EnvValue -Env $config -Key "SMTP_HOST"
$smtpPort = [int](Require-EnvValue -Env $config -Key "SMTP_PORT")
$smtpUser = Require-EnvValue -Env $config -Key "SMTP_USER"
$smtpPass = Require-EnvValue -Env $config -Key "SMTP_PASS"
$smtpFrom = Require-EnvValue -Env $config -Key "SMTP_FROM"
$smtpToRaw = Require-EnvValue -Env $config -Key "SMTP_TO"

$useSsl = $true
if ($config.ContainsKey("SMTP_USE_SSL")) {
    $useSsl = ([string]$config["SMTP_USE_SSL"]).Trim().ToLowerInvariant() -in @("1", "true", "yes", "y")
}

if (-not (Test-Path $RunDir)) {
    throw "RunDir not found: $RunDir"
}

$reviewPath = Get-OptionalFile (Join-Path $RunDir "review.json")
$ocStockPath = Get-OptionalFile (Join-Path $RunDir "oc_stock.csv")
$summaryPath = Get-OptionalFile (Join-Path $RunDir "summary.csv")
$bridgeLogPath = Get-OptionalFile (Join-Path $RunDir "bridge.log")

$review = $null
if ($reviewPath) {
    try {
        $review = Get-Content $reviewPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($review.status) {
            $Status = [string]$review.status
        }
    } catch {
        Write-EmailLog "WARNING: Could not parse review.json"
    }
}

$subject = "$SubjectPrefix $Status"

$counts = Get-JsonProp -Object $review -Name "counts" -Default $null

if ($counts) {
    $subject = "$SubjectPrefix $Status - rows=$(Get-JsonProp $counts 'output_rows' 0), disabled=$(Get-JsonProp $counts 'disabled_new_count' 0)"
}

$summaryTableHtml = Convert-SummaryCsvToHtmlTable -SummaryPath $summaryPath -MaxRows $MaxSummaryRowsInBody
$priceZeroTableHtml = Convert-PriceZeroProductsToHtmlTable -Review $review -MaxRows 200

$hardFailuresHtml = "<li>none</li>"
$warningsHtml = "<li>none</li>"

if ($review -and $review.safety) {
    if ($review.safety.hard_failures -and $review.safety.hard_failures.Count -gt 0) {
        $items = foreach ($item in $review.safety.hard_failures) {
            "<li>$(HtmlEncode $item)</li>"
        }
        $hardFailuresHtml = $items -join "`n"
    }

    if ($review.safety.warnings -and $review.safety.warnings.Count -gt 0) {
        $items = foreach ($item in $review.safety.warnings) {
            "<li>$(HtmlEncode $item)</li>"
        }
        $warningsHtml = $items -join "`n"
    }
}

$countHtml = ""

if ($counts) {
    $countHtml = @"
<table cellpadding="3" cellspacing="0" border="1" style="border-collapse:collapse; font-family:Arial, sans-serif; font-size:12px; line-height:1.15;">
  <tr><td style="padding:3px 6px;"><strong>Output rows</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'output_rows' 0)</td></tr>
  <tr><td style="padding:3px 6px;"><strong>Quantity changed</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'quantity_changed_count' 0)</td></tr>
  <tr><td style="padding:3px 6px;"><strong>Status would change</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'status_would_change_count' 0)</td></tr>
  <tr><td style="padding:3px 6px;"><strong>Disabled new count</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'disabled_new_count' 0)</td></tr>
  <tr><td style="padding:3px 6px;"><strong>Price zero forced disabled</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'price_zero_forced_disabled_count' 0)</td></tr>
  <tr><td style="padding:3px 6px;"><strong>Disabled ratio</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'disabled_ratio_percent' 0)%</td></tr>
  <tr><td style="padding:3px 6px;"><strong>Stock rows</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'stock_rows' 0)</td></tr>
  <tr><td style="padding:3px 6px;"><strong>OpenCart rows</strong></td><td style="padding:3px 6px;" align="right">$(Get-JsonProp $counts 'opencart_rows' 0)</td></tr>
</table>
"@
}

$body = @"
<html>
<body style="font-family:Arial, sans-serif; font-size:14px; color:#222;">
  <h1 style="font-size:22px; margin-bottom:6px;">OpenCart Stock Sync Report</h1>

  <p>
    <strong>Status:</strong> $(HtmlEncode $Status)<br />
    <strong>Run dir:</strong> $(HtmlEncode $RunDir)<br />
    <strong>Created:</strong> $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  </p>

  <h2 style="font-size:17px; margin-top:18px; margin-bottom:8px;">Counts</h2>
  $countHtml

  $priceZeroTableHtml

  $summaryTableHtml

  <h2 style="font-size:17px; margin-top:22px; margin-bottom:8px;">Safety</h2>
  <p style="margin:0 0 4px 0;"><strong>Hard failures:</strong></p>
  <ul style="margin-top:4px;">
    $hardFailuresHtml
  </ul>

  <p style="margin:10px 0 4px 0;"><strong>Warnings:</strong></p>
  <ul style="margin-top:4px;">
    $warningsHtml
  </ul>

  <h2 style="font-size:17px; margin-top:24px; margin-bottom:8px;">Attachments</h2>
  <ul>
    <li><strong>oc_stock.csv:</strong> $(HtmlEncode $ocStockPath)</li>
    <li><strong>summary.csv:</strong> $(HtmlEncode $summaryPath)</li>
    <li><strong>review.json:</strong> $(HtmlEncode $reviewPath)</li>
    <li><strong>bridge.log:</strong> $(HtmlEncode $bridgeLogPath)</li>
  </ul>
</body>
</html>
"@

$message = New-Object System.Net.Mail.MailMessage
$message.From = $smtpFrom

$smtpToRaw.Split(",") | ForEach-Object {
    $addr = $_.Trim()
    if (-not [string]::IsNullOrWhiteSpace($addr)) {
        [void]$message.To.Add($addr)
    }
}

$message.Subject = $subject
$message.Body = $body
$message.IsBodyHtml = $true

$attachments = @(
    $ocStockPath,
    $summaryPath,
    $reviewPath,
    $bridgeLogPath
) | Where-Object { $_ -and (Test-Path $_) }

foreach ($attachmentPath in $attachments) {
    Write-EmailLog "Attaching: $attachmentPath"
    [void]$message.Attachments.Add((New-Object System.Net.Mail.Attachment($attachmentPath)))
}

$client = New-Object System.Net.Mail.SmtpClient($smtpHost, $smtpPort)
$client.EnableSsl = $useSsl
$client.Credentials = New-Object System.Net.NetworkCredential($smtpUser, $smtpPass)

try {
    Write-EmailLog "Sending email to $smtpToRaw via ${smtpHost}:${smtpPort} ssl=$useSsl"
    $client.Send($message)
    Write-EmailLog "Email sent"
} finally {
    $message.Dispose()
    $client.Dispose()
}

Write-Host ""
Write-Host "Email sent"
Write-Host "----------"
Write-Host "To:      $smtpToRaw"
Write-Host "Subject: $subject"
Write-Host "Log:     $EmailLog"
Write-Host ""