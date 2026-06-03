Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-UtcNowIso {
  (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Invoke-JsonGet([string]$Url) {
  Invoke-RestMethod -Method Get -Uri $Url -TimeoutSec 20 -Headers @{ "User-Agent" = "Mozilla/5.0" }
}

function Format-Num([double]$Value) {
  if ($Value -ge 1) { return "{0:N2}" -f $Value }
  return ("{0:F6}" -f $Value).TrimEnd("0").TrimEnd(".")
}

function Nice-Level([double]$Level) {
  $x = [double]$Level
  if ($x -ge 1000) { return ([Math]::Round($x, 0)).ToString("N0") }
  if ($x -ge 100) { return ([Math]::Round($x, 1)).ToString("N1") }
  if ($x -ge 10) { return ([Math]::Round($x, 2)).ToString("N2") }
  if ($x -ge 1) { return ([Math]::Round($x, 3)).ToString("N3") }
  return (Format-Num $x)
}

function Get-Binance24h([string[]]$Symbols) {
  $base = ($env:BINANCE_API_BASE)
  if ([string]::IsNullOrWhiteSpace($base)) { $base = "https://api.binance.com" }
  $base = $base.TrimEnd("/")

  $out = @()
  foreach ($sym in $Symbols) {
    $url = "$base/api/v3/ticker/24hr?symbol=$sym"
    $raw = Invoke-JsonGet $url
    $out += [pscustomobject]@{
      symbol      = [string]$raw.symbol
      last        = [double]$raw.lastPrice
      changePct   = [double]$raw.priceChangePercent
      high        = [double]$raw.highPrice
      low         = [double]$raw.lowPrice
      quoteVolume = [double]$raw.quoteVolume
      sourceUrl   = $url
    }
  }
  return $out
}

function Get-Binance24hAll {
  $base = ($env:BINANCE_API_BASE)
  if ([string]::IsNullOrWhiteSpace($base)) { $base = "https://api.binance.com" }
  $base = $base.TrimEnd("/")

  $url = "$base/api/v3/ticker/24hr"
  $raw = Invoke-JsonGet $url
  return @($raw | ForEach-Object {
    [pscustomobject]@{
      symbol      = [string]$_.symbol
      last        = [double]$_.lastPrice
      changePct   = [double]$_.priceChangePercent
      high        = [double]$_.highPrice
      low         = [double]$_.lowPrice
      quoteVolume = [double]$_.quoteVolume
      sourceUrl   = $url
    }
  })
}

function Make-Binance24hPost($t) {
  $base = [string]$t.symbol
  if ($base.EndsWith("USDT")) { $base = $base.Substring(0, $base.Length - 4) }
  $coin = "`$$base"

  $priceText = Format-Num $t.last
  $highText = Format-Num $t.high
  $lowText = Format-Num $t.low
  $chg = "{0:+0.00;-0.00;+0.00}%" -f ([double]$t.changePct)

  $upBreak = [double]$t.high
  $downBreak = [double]$t.low
  $up1 = $upBreak * 1.01
  $up2 = $upBreak * 1.02
  $down1 = $downBreak * 0.99
  $down2 = $downBreak * 0.98

  $nearHigh = (($t.high - $t.last) / [Math]::Max($t.high, 0.0000001)) -lt 0.003
  $nearLow = (($t.last - $t.low) / [Math]::Max($t.low, 0.0000001)) -lt 0.003

  $hook =
    if ($nearHigh) { "Near 24h high; watch breakout." }
    elseif ($nearLow) { "Near 24h low; watch breakdown." }
    elseif ([Math]::Abs([double]$t.changePct) -ge 3) { "Volatile; do not chase." }
    else { "Range; level matters." }

  @"
$coin $hook
Price $priceText | 24h $chg | High $highText | Low $lowText

Up path: $(Nice-Level $upBreak) -> $(Nice-Level $up1) -> $(Nice-Level $up2)
Down path: $(Nice-Level $downBreak) -> $(Nice-Level $down1) -> $(Nice-Level $down2)

Action: no chasing; try after pullback holds; exit if low breaks.
"@.Trim()
}

function Append-Draft([string]$Source, [string]$Url, [string[]]$Keywords, [string]$Body) {
  $draftId = (Get-Date).ToString("yyyyMMddHHmmss")
  $record = [ordered]@{
    id         = $draftId
    created_at = (Get-UtcNowIso)
    source     = $Source
    url        = $Url
    keywords   = $Keywords
    body       = $Body
    posted     = $false
  }
  $path = Join-Path -Path (Get-Location) -ChildPath "square_drafts.jsonl"
  ($record | ConvertTo-Json -Compress -Depth 6) | Add-Content -Path $path -Encoding UTF8
  return $draftId
}

function Choose-Posts($mainTickers, $allTickers, [int]$MaxPosts = 3) {
  $preferredOrder = @("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "TRXUSDT")
  $mainBySymbol = @{}
  foreach ($t in $mainTickers) { $mainBySymbol[$t.symbol] = $t }

  $rankedMain = @()
  foreach ($sym in $preferredOrder) {
    if (-not $mainBySymbol.ContainsKey($sym)) { continue }
    $t = $mainBySymbol[$sym]
    $nearHigh = (($t.high - $t.last) / [Math]::Max($t.high, 0.0000001)) -lt 0.003
    $nearLow = (($t.last - $t.low) / [Math]::Max($t.low, 0.0000001)) -lt 0.003
    $score = [Math]::Abs([double]$t.changePct) + $(if ($nearHigh -or $nearLow) { 2 } else { 0 })
    $rankedMain += [pscustomobject]@{ t = $t; score = $score }
  }
  $rankedMain = $rankedMain | Sort-Object -Property score -Descending

  $picked = @()
  foreach ($x in $rankedMain) {
    if ($picked.Count -ge [Math]::Min(2, $MaxPosts)) { break }
    $picked += $x.t
  }

  if ($picked.Count -lt $MaxPosts) {
    $exclude = @($preferredOrder + ($picked | ForEach-Object { $_.symbol }))
    $gainer = $allTickers |
    Where-Object {
      $_.symbol.EndsWith("USDT") -and
      -not ($exclude -contains $_.symbol) -and
      $_.quoteVolume -ge 2000000 -and
      $_.changePct -ge 6 -and
      $_.symbol -notmatch "^(USDC|FDUSD|TUSD|DAI|USDP|USDS)USDT$" -and
      $_.symbol -notmatch "(UP|DOWN|BULL|BEAR)USDT$"
    } |
    Sort-Object -Property changePct -Descending |
    Select-Object -First 1

    if ($null -ne $gainer) { $picked += $gainer }
  }

  return $picked | Select-Object -First $MaxPosts
}

$Command = if ($args.Count -ge 1 -and -not [string]::IsNullOrWhiteSpace([string]$args[0])) { [string]$args[0] } else { "run" }
if ($Command -ne "run") { throw "Unsupported command: $Command (only: run)" }

$mainSymbols = @("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "TRXUSDT")
$main = Get-Binance24h -Symbols $mainSymbols
$all = Get-Binance24hAll
$picked = Choose-Posts -mainTickers $main -allTickers $all -MaxPosts 3

$drafts = @()
foreach ($t in $picked) {
  $body = Make-Binance24hPost $t
  $id = Append-Draft -Source "binance_24h_ps" -Url $t.sourceUrl -Keywords @($t.symbol) -Body $body
  $drafts += [pscustomobject]@{ id = $id; symbol = $t.symbol; url = $t.sourceUrl; body = $body }
}

[pscustomobject]@{
  created_at   = (Get-UtcNowIso)
  used_symbols = @($picked | ForEach-Object { $_.symbol })
  drafts       = $drafts
} | ConvertTo-Json -Depth 8


