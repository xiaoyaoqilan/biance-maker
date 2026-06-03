Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DraftsPath = Join-Path $AppDir "square_drafts.jsonl"
$PostLogPath = Join-Path $AppDir "square_post_log.jsonl"
$StatePath = Join-Path $AppDir "square_github_state.json"
$Endpoint = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
$MarketEndpoint = "https://data-api.binance.vision/api/v3/ticker/24hr"
$DailyLimit = 60
$RetentionHours = 48

function UtcNow {
  return (Get-Date).ToUniversalTime()
}

function Load-State {
  if (Test-Path $StatePath) {
    try { return Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch {}
  }
  return [pscustomobject]@{
    day = (UtcNow).ToString("yyyy-MM-dd")
    posted_today = 0
    last_cleanup = "2000-01-01T00:00:00Z"
  }
}

function Save-State($State) {
  $State | ConvertTo-Json -Depth 8 | Set-Content -Path $StatePath -Encoding UTF8
}

function Reset-DayIfNeeded($State) {
  $today = (UtcNow).ToString("yyyy-MM-dd")
  if ($State.day -ne $today) {
    $State.day = $today
    $State.posted_today = 0
  }
}

function Cleanup-OldFiles($State) {
  $last = [datetime]"2000-01-01T00:00:00Z"
  try { $last = [datetime]$State.last_cleanup } catch {}
  if (((UtcNow) - $last).TotalHours -lt 48) { return }

  $cutoff = (Get-Date).AddHours(-$RetentionHours)
  foreach ($pattern in @("square_posts_preview_*.jsonl", "_binance24h_snapshot_*.json", "square_round_*.json", "_round_out.json")) {
    Get-ChildItem -Path $AppDir -Filter $pattern -File -ErrorAction SilentlyContinue |
      Where-Object { $_.LastWriteTime -lt $cutoff } |
      Remove-Item -Force -ErrorAction SilentlyContinue
  }

  foreach ($path in @($DraftsPath, $PostLogPath)) {
    if ((Test-Path $path) -and ((Get-Item $path).LastWriteTime -lt $cutoff)) {
      Remove-Item -Force $path -ErrorAction SilentlyContinue
      New-Item -ItemType File -Force -Path $path | Out-Null
    }
  }

  $State.last_cleanup = (UtcNow).ToString("s") + "Z"
  Save-State $State
}

function ConvertTo-JsonLine($Object) {
  return ($Object | ConvertTo-Json -Compress -Depth 20)
}

function Read-Jsonl($Path) {
  if (-not (Test-Path $Path)) { return @() }
  $items = @()
  foreach ($line in Get-Content $Path -Encoding UTF8 -ErrorAction SilentlyContinue) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try { $items += ($line | ConvertFrom-Json) } catch {}
  }
  return $items
}

function Get-UnpostedDraft {
  $drafts = @(Read-Jsonl $DraftsPath | Where-Object {
    -not $_.posted -and -not [string]::IsNullOrWhiteSpace([string]$_.body)
  })
  if ($drafts.Count -gt 0) { return $drafts[0] }
  return $null
}

function Get-RecentTickers {
  $recent = @{}
  $cutoff = (UtcNow).AddHours(-48)
  foreach ($record in @(Read-Jsonl $PostLogPath)) {
    try {
      $created = [datetime]$record.created_at
      if ($created -lt $cutoff) { continue }
      $body = [string]$record.body
      foreach ($m in [regex]::Matches($body, '\$([A-Z0-9]{2,15})')) {
        $recent[$m.Groups[1].Value.ToUpperInvariant()] = $true
      }
    } catch {}
  }
  return $recent
}

function Format-Price([double]$Value) {
  $abs = [Math]::Abs($Value)
  if ($abs -ge 1000) { return $Value.ToString("0") }
  if ($abs -ge 100) { return $Value.ToString("0.0") }
  if ($abs -ge 1) { return $Value.ToString("0.###") }
  if ($abs -ge 0.1) { return $Value.ToString("0.####") }
  return $Value.ToString("0.########")
}

function Format-Volume([double]$Value) {
  if ($Value -ge 1000000000) { return (($Value / 1000000000).ToString("0.##") + "B") }
  if ($Value -ge 1000000) { return (($Value / 1000000).ToString("0.##") + "M") }
  if ($Value -ge 1000) { return (($Value / 1000).ToString("0.##") + "K") }
  return $Value.ToString("0")
}

function New-DraftFromMarket {
  $raw = Invoke-RestMethod -Method Get -Uri $MarketEndpoint -TimeoutSec 45 -Headers @{ "User-Agent" = "Mozilla/5.0" }
  $recent = Get-RecentTickers
  $stable = @("USDC", "FDUSD", "TUSD", "DAI", "USDP", "USDS")
  $candidates = @($raw | Where-Object {
    [string]$_.symbol -like "*USDT" -and
    [double]$_.quoteVolume -ge 5000000 -and
    [Math]::Abs([double]$_.priceChangePercent) -le 80
  } | ForEach-Object {
    $base = ([string]$_.symbol).Substring(0, ([string]$_.symbol).Length - 4).ToUpperInvariant()
    if ($stable -contains $base) { return }
    if ($base -match "(UP|DOWN|BULL|BEAR)$") { return }
    if ($recent.ContainsKey($base)) { return }
    [pscustomobject]@{
      base = $base
      symbol = [string]$_.symbol
      last = [double]$_.lastPrice
      changePct = [double]$_.priceChangePercent
      high = [double]$_.highPrice
      low = [double]$_.lowPrice
      quoteVolume = [double]$_.quoteVolume
      score = [Math]::Abs([double]$_.priceChangePercent) + ([Math]::Log10([double]$_.quoteVolume) / 2)
    }
  } | Where-Object { $null -ne $_ } | Sort-Object -Property score -Descending)

  if ($candidates.Count -eq 0) { throw "No market candidates available." }
  $t = $candidates[0]
  $chg = if ($t.changePct -ge 0) { "+" + $t.changePct.ToString("0.00") + "%" } else { $t.changePct.ToString("0.00") + "%" }
  $coin = '$' + $t.base
  $bias = if ($t.changePct -ge 0) { "strong, but no chase" } else { "weak tape, wait for support" }
  $body = @"
$coin now $(Format-Price $t.last), 24h $chg

High $(Format-Price $t.high), low $(Format-Price $t.low), volume $(Format-Volume $t.quoteVolume) USDT.

Plan: $bias. Break high = watch continuation. Lose low = step aside.

Not financial advice. Levels and volume first.
"@.Trim()

  $draft = [ordered]@{
    id = ((UtcNow).ToString("yyyyMMddHHmmss") + "_" + $t.symbol)
    created_at = (UtcNow).ToString("s") + "Z"
    source = "github-market-auto"
    url = $MarketEndpoint
    keywords = @($t.symbol)
    body = $body
    posted = $false
    realtime = @{ ticker = $t }
  }
  ConvertTo-JsonLine $draft | Add-Content -Path $DraftsPath -Encoding UTF8
  return ($draft | ConvertTo-Json -Depth 20 | ConvertFrom-Json)
}

function Rewrite-DraftAsPosted([string]$DraftId, $Result) {
  if (-not (Test-Path $DraftsPath)) { return }
  $lines = @()
  foreach ($line in Get-Content $DraftsPath -Encoding UTF8 -ErrorAction SilentlyContinue) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try {
      $item = $line | ConvertFrom-Json
      if ([string]$item.id -eq [string]$DraftId) {
        $item | Add-Member -Force -NotePropertyName posted -NotePropertyValue $true
        $item | Add-Member -Force -NotePropertyName posted_at -NotePropertyValue ((UtcNow).ToString("s") + "Z")
        $item | Add-Member -Force -NotePropertyName post_result -NotePropertyValue $Result
      }
      $lines += (ConvertTo-JsonLine $item)
    } catch {
      $lines += $line
    }
  }
  $lines | Set-Content -Path $DraftsPath -Encoding UTF8
}

function Append-PostLog([string]$Body, $Result, [string]$Source) {
  $postId = $null
  if ($null -ne $Result.data -and $Result.data.PSObject.Properties.Name -contains "id") {
    $postId = [string]$Result.data.id
  }
  $record = [ordered]@{
    created_at = (UtcNow).ToString("s") + "Z"
    source = $Source
    body = $Body
    result = $Result
    post_id = $postId
    post_url = if ($postId) { "https://www.binance.com/square/post/$postId" } else { $null }
  }
  ConvertTo-JsonLine $record | Add-Content -Path $PostLogPath -Encoding UTF8
}

function Publish-Text([string]$Body) {
  $key = [string]$env:BINANCE_SQUARE_OPENAPI_KEY
  if ([string]::IsNullOrWhiteSpace($key)) {
    throw "Missing BINANCE_SQUARE_OPENAPI_KEY secret."
  }
  $payload = @{ bodyTextOnly = $Body } | ConvertTo-Json -Compress
  return Invoke-RestMethod -Method Post -Uri $Endpoint -TimeoutSec 45 -Headers @{
    "X-Square-OpenAPI-Key" = $key
    "Content-Type" = "application/json; charset=utf-8"
    "clienttype" = "binanceSkill"
    "User-Agent" = "Mozilla/5.0"
  } -Body $payload
}

Set-Location $AppDir
$state = Load-State
Reset-DayIfNeeded $state
Cleanup-OldFiles $state

if ([int]$state.posted_today -ge $DailyLimit) {
  Write-Host "Daily limit reached: $($state.posted_today)/$DailyLimit"
  Save-State $state
  exit 0
}

$draft = Get-UnpostedDraft
if ($null -eq $draft) {
  Write-Host "No unposted drafts. Generating one from market data."
  $draft = New-DraftFromMarket
}

$body = [string]$draft.body
$result = Publish-Text $body
Append-PostLog -Body $body -Result $result -Source "github-actions"
Rewrite-DraftAsPosted -DraftId ([string]$draft.id) -Result $result

$state.posted_today = [int]$state.posted_today + 1
$state.last_post = (UtcNow).ToString("s") + "Z"
Save-State $state

$postId = ""
if ($null -ne $result.data -and $result.data.PSObject.Properties.Name -contains "id") {
  $postId = [string]$result.data.id
}
Write-Host "Posted code=$($result.code) id=$postId posted_today=$($state.posted_today)/$DailyLimit"
if ($postId) {
  Write-Host "https://www.binance.com/square/post/$postId"
}
