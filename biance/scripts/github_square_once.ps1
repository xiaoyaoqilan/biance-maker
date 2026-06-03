Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DraftsPath = Join-Path $AppDir "square_drafts.jsonl"
$PostLogPath = Join-Path $AppDir "square_post_log.jsonl"
$StatePath = Join-Path $AppDir "square_github_state.json"
$Endpoint = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
$MarketEndpoint = "https://data-api.binance.vision/api/v3/ticker/24hr"
$DailyLimit = 60
$RunIntervalMinutes = 10
$MainSymbols = @("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
$StableBases = @("USDC", "FDUSD", "TUSD", "DAI", "USDP", "USDS")

function UtcNow { (Get-Date).ToUniversalTime() }
function JsonLine($x) { $x | ConvertTo-Json -Compress -Depth 30 }
function Has-Prop($obj, [string]$name) { $obj.PSObject.Properties.Name -contains $name }

function Read-Jsonl($Path) {
  if (-not (Test-Path $Path)) { return @() }
  $items = @()
  foreach ($line in Get-Content $Path -Encoding UTF8 -ErrorAction SilentlyContinue) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    try { $items += ($line | ConvertFrom-Json) } catch {}
  }
  return $items
}

function Load-State {
  if (Test-Path $StatePath) {
    try { return Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
  }
  [pscustomobject]@{ day = (UtcNow).ToString("yyyy-MM-dd"); posted_today = 0; last_post = $null }
}

function Save-State($s) { $s | ConvertTo-Json -Depth 10 | Set-Content $StatePath -Encoding UTF8 }

function Reset-Day($s) {
  $today = (UtcNow).ToString("yyyy-MM-dd")
  if ($s.day -ne $today) {
    $s.day = $today
    $s.posted_today = 0
    $s.last_post = $null
  }
}

function Format-Price([double]$v) {
  $a = [Math]::Abs($v)
  if ($a -ge 1000) { return $v.ToString("0") }
  if ($a -ge 100) { return $v.ToString("0.0") }
  if ($a -ge 1) { return $v.ToString("0.###") }
  if ($a -ge 0.1) { return $v.ToString("0.####") }
  return $v.ToString("0.########")
}

function Format-Vol([double]$v) {
  if ($v -ge 1000000000) { return (($v / 1000000000).ToString("0.##") + "B") }
  if ($v -ge 1000000) { return (($v / 1000000).ToString("0.##") + "M") }
  if ($v -ge 1000) { return (($v / 1000).ToString("0.##") + "K") }
  return $v.ToString("0")
}

function Normalize-Ticker($x) {
  $symbol = [string]$x.symbol
  if (-not $symbol.EndsWith("USDT")) { return $null }
  $base = $symbol.Substring(0, $symbol.Length - 4).ToUpperInvariant()
  if ($StableBases -contains $base) { return $null }
  if ($base -match "(UP|DOWN|BULL|BEAR)$") { return $null }
  $qv = [double]$x.quoteVolume
  $chg = [double]$x.priceChangePercent
  if ($qv -lt 5000000) { return $null }
  if ([Math]::Abs($chg) -gt 120) { return $null }
  [pscustomobject]@{
    base = $base
    symbol = $symbol
    last = [double]$x.lastPrice
    changePct = $chg
    high = [double]$x.highPrice
    low = [double]$x.lowPrice
    quoteVolume = $qv
  }
}

function Build-Candidates {
  $raw = Invoke-RestMethod -Method Get -Uri $MarketEndpoint -TimeoutSec 45 -Headers @{ "User-Agent" = "Mozilla/5.0" }
  $all = @()
  foreach ($x in $raw) {
    $t = Normalize-Ticker $x
    if ($null -ne $t) { $all += $t }
  }

  $main = @()
  foreach ($sym in $MainSymbols) {
    $hit = $all | Where-Object { $_.symbol -eq $sym } | Select-Object -First 1
    if ($null -ne $hit) { $hit | Add-Member -Force -NotePropertyName bucket -NotePropertyValue "main"; $main += $hit }
  }

  $gainers = @($all | Where-Object { $_.changePct -gt 0 } | Sort-Object changePct -Descending | Select-Object -First 5)
  foreach ($t in $gainers) { $t | Add-Member -Force -NotePropertyName bucket -NotePropertyValue "gainer" }
