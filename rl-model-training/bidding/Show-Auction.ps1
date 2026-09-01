# Dot-source it, then call Show-Auction:
#
#     . .\Show-Auction.ps1
#     Show-Auction 'a ward bed is free'
#     Show-Auction 'one limited ICU bed' -Scenario ward_crash
#
# A dev view over POST /auction. The full response has ~25 top-level fields; this keeps the
# five that answer "who got it and why" — the same five the server now renders itself at
# ?format=text, which is one curl and no PowerShell. For everything else use -Raw, or
# ?format=report for the step-by-step derivation.

[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Show-Auction {
    param(
        [Parameter(Mandatory, Position = 0)][string]$Query,
        [string]$Scenario,
        [switch]$Raw,
        [string]$Uri = 'http://localhost:8000/auction'
    )

    $payload = @{ query = $Query }
    if ($Scenario) { $payload.scenario = $Scenario }

    try {
        $r = Invoke-RestMethod -Uri $Uri -Method Post -ContentType 'application/json' `
                               -Body ($payload | ConvertTo-Json)
    }
    catch {
        # A refused query is a 400 carrying the reason. Without this the useful message is
        # buried in an exception and all you see is "400 Bad Request".
        $body = $_.ErrorDetails.Message
        if ($body) { Write-Warning (($body | ConvertFrom-Json).error) } else { throw }
        return
    }

    if ($Raw) { return $r }

    $reserve = [math]::Round($r.reserve_price, 1)
    $cleared = if ($r.winning_bid -ge $r.reserve_price) { 'cleared' } else { 'BELOW RESERVE' }
    $ranked = ($r.utilities.PSObject.Properties |
               Sort-Object { - $_.Value } |
               ForEach-Object { "$($_.Name.Split('-')[0].ToLower()) $([math]::Round($_.Value, 1))" }) -join ' | '

    [pscustomobject][ordered]@{
        query     = $r.query
        resource  = $r.resource.type
        outcome   = $r.outcome
        winner    = "$($r.winner) ($($r.winning_candidate_id))  bid $($r.winning_bid)   reserve $reserve   $cleared"
        utilities = $ranked
    }
}
