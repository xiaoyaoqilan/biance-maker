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
$Disclaimer = "仅为个人观察，不构成投资建议。"

function UtcNow { (Get-Date).ToUniversalTime() }

function Load-State {
    if (Test-Path $StatePath) {
        try { return Get-Content $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
    }
    [pscustomobject]@{ day = (UtcNow).ToString("yyyy-MM-dd"); posted_today = 0; last_post = $null }
}

function Save-State($s) {
    [System.IO.File]::WriteAllText($StatePath, ($s | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
}

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

    $losers = @($all | Where-Object { $_.changePct -lt 0 } | Sort-Object changePct | Select-Object -First 5)
    foreach ($t in $losers) { $t | Add-Member -Force -NotePropertyName bucket -NotePropertyValue "loser" }

    $combined = @() + $main + $gainers + $losers
    return $combined
}

function Format-Post($t) {
    $coin = '$' + $t.base
    $price = Format-Price $t.last
    $high = Format-Price $t.high
    $low = Format-Price $t.low
    $chg = ($t.changePct).ToString("+0.00;-0.00") + "%"

    $hooks = @(
        "兄弟们，这票有妖气，主力要开始表演了。",
        "BNB 这个位置，狗庄插针插得我头皮发麻。",
        "还在无脑看多？别被卖了还帮人数钱，看清楚点位。",
        "这行情，散户现在进去就是送外卖，速度撤离。",
        "我盯这票一整晚了，主力意图已经图穷匕见。",
        "这种走势，摆明了是要收割最后一波韭菜。",
        "别跟我谈什么信仰，在这个点位，保命才是第一位。",
        "这就是标准的诱多陷阱，谁冲谁接盘。",
        "盘面异动极其明显，大行情可能就在这一两个小时。",
        "我看这票要变天了，还没清醒的赶紧看过来。"
    )
    $hook = $hooks | Get-Random

    $insights = @(
        "听我一句，上看 $(Format-Price $t.high) 冲不过去就是骗炮，别硬扛，容易爆仓。",
        "真要跌破 $(Format-Price $t.low) 直接就是瀑布，别犹豫，止损比命长。",
        "现在这位置，主力在疯狂洗盘，你要是怂了就真被洗出去了。",
        "这种震荡就是在磨耐心，点位不到绝对不入场，懂的都懂。",
        "缩量阴跌最致命，关注这个支撑位，破了就是深渊。",
        "别死盯着K线看，看大户的成交量，主力在悄悄撤退。",
        "这波我看还要往下扎针，没上车的别乱冲，等狗庄表演完。"
    )
    $insight = $insights | Get-Random

    $ctas = @(
        "想看我下波盯谁？评论区打出来，速度点关注，别等爆仓了再来！",
        "觉得我看得准的，点赞支持一下，下波行情带你们避坑。",
        "这种干货也就我会说，点个关注不迷路，财富自由看这波。",
        "关注我，带你拆解狗庄的所有套路，评论区见！",
        "行情不等人，速度点关注，实时盯盘带你飞。"
    )
    $cta = $ctas | Get-Random

    $templates = @(
        "$coin $hook`n`n现在价格 $price，24h $chg。最高 $high，最低 $low。`n`n点位建议：上看 $(Format-Price $t.high) 压力，下看 $(Format-Price $t.low) 支撑。`n`n$insight`n`n$cta`n`n$Disclaimer",
        "$hook $coin 现在的局面很尴尬。`n`n目前 $price，24小时波动 $chg。高位 $high，低位 $low。`n`n点位死盯：压力位 $(Format-Price $t.high)，支撑位 $(Format-Price $t.low)。破位直接走人，别谈格局。`n`n$insight`n`n$cta`n`n$Disclaimer",
        "又是被狗庄支配的一天？看看 $coin。`n`n现价 $price ($chg)。今天高低点：$high / $low。`n`n我的建议：冲破 $(Format-Price $t.high) 再看涨，不然全是骗炮。跌破 $(Format-Price $t.low) 赶紧跑路。`n`n$insight`n`n$cta`n`n$Disclaimer"
    )
    return ($templates | Get-Random).Trim()
}

function Post-Text($body) {
    $json = @{ bodyTextOnly = $body } | ConvertTo-Json -Compress -Depth 10
    $hdr = @{
        "X-Square-OpenAPI-Key" = $env:BINANCE_SQUARE_OPENAPI_KEY
        "Content-Type" = "application/json"
        "clienttype" = "binanceSkill"
    }
    return Invoke-RestMethod -Method Post -Uri $Endpoint -Body $json -Headers $hdr -TimeoutSec 30
}

function Log-Post($body, $postId) {
    $rec = [pscustomobject]@{
        created_at = (UtcNow).ToString("o")
        source = "github_square_auto"
        body = $body
        post_id = $postId
    }
    $line = ($rec | ConvertTo-Json -Compress -Depth 20)
    [System.IO.File]::AppendAllText($PostLogPath, $line + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}

# ---- main ----
$state = Load-State
Reset-Day $state
$now = UtcNow

if (-not [string]::IsNullOrEmpty($state.last_post)) {
    try {
        $last = [datetime]::Parse($state.last_post, $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
        if (($now - $last).TotalMinutes -lt $RunIntervalMinutes) {
            Write-Host "Within run interval ($RunIntervalMinutes min); skipping post."
            exit 0
        }
    } catch { }
}

if ([int]$state.posted_today -ge $DailyLimit) {
    Write-Host "Daily limit ($DailyLimit) reached; skipping post."
    exit 0
}

$cands = Build-Candidates
if ($null -eq $cands -or $cands.Count -eq 0) {
    Write-Host "No candidates from market; skipping post."
    exit 0
}

$pick = $cands | Get-Random
$body = Format-Post $pick
Write-Host "Posting about $($pick.symbol) (bucket=$($pick.bucket))..."

try {
    $result = Post-Text $body
    $postId = $null
    if ($null -ne $result -and $null -ne $result.data -and $null -ne $result.data.id) { $postId = $result.data.id }
    Log-Post $body $postId
    $state.posted_today = [int]$state.posted_today + 1
    $state.last_post = $now.ToString("o")
    Save-State $state
    Write-Host "Posted successfully. posted_today=$($state.posted_today)"
    exit 0
} catch {
    Write-Error "Post failed: $_"
    $errRec = [pscustomobject]@{
        created_at = (UtcNow).ToString("o")
        source = "github_square_auto"
        error = $_.ToString()
        body = $body
    }
    [System.IO.File]::AppendAllText($PostLogPath, (($errRec | ConvertTo-Json -Compress -Depth 20) + [Environment]::NewLine), [System.Text.UTF8Encoding]::new($false))
    exit 1
}
