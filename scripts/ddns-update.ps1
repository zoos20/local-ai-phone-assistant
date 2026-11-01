#Requires -Version 5.1
<#
    Updates the dynv6 AAAA record with the PC's stable global IPv6, and the
    A record with the current public IPv4 (plan "hybrid tunnel": the phone's
    mobile network is IPv4-only, so the A record is what carries the tunnel
    off-home; the provider prefix/address changes, DDNS follows).

    Reads <project>\server\.env (python config lives in server\):
      ASSISTANT_DDNS_HOSTNAME  -- zone, e.g. mypc.dynv6.net
      ASSISTANT_DDNS_TOKEN     -- dynv6 token (https://dynv6.com -> API)

    IPv6 choice: a Preferred, non-link-local address whose SuffixOrigin is
    LinkLayerAddress (the stable "Link" suffix from the plan, NOT the
    Random privacy extension). Run every 5 minutes via
    scripts\setup-ddns-task.ps1.

    ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
    PowerShell 5.1, and non-ASCII characters in it break the parser.
#>

# IPv4 is sent by default: the phone's mobile network is IPv4-only, so the
# A record is what actually carries the tunnel off-home. Pass -NoIPv4 to
# update AAAA only (e.g. if the PC sits behind CGNAT with no port forward).
param([switch]$NoIPv4)

$ErrorActionPreference = 'Stop'

function Read-EnvValue([string]$Key) {
    $envPath = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
    if (-not (Test-Path $envPath)) { return $null }
    foreach ($line in Get-Content $envPath) {
        if ($line -match "^\s*$Key\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim()
        }
    }
    return $null
}

$hostname = Read-EnvValue 'ASSISTANT_DDNS_HOSTNAME'
$token    = Read-EnvValue 'ASSISTANT_DDNS_TOKEN'
if (-not $hostname -or -not $token) {
    Write-Host '[ddns] ASSISTANT_DDNS_HOSTNAME/ASSISTANT_DDNS_TOKEN not set in .env'
    exit 1
}

# Stable global IPv6: no fe80:: zone, Preferred state, stable suffix.
# NB: Windows reports the stable EUI-64 suffix as SuffixOrigin "Link"
# (the plan's "Link, not Random"); the privacy extension is "Random".
$candidates = @(Get-NetIPAddress -AddressFamily IPv6 -AddressState Preferred -ErrorAction SilentlyContinue |
    Where-Object {
        $_.IPAddress -notlike 'fe80*' -and
        $_.IPAddress -notlike 'fd*' -and   # ULA (fd00::/8) is not global
        $_.IPAddress -ne '::1' -and
        $_.SuffixOrigin -ne 'Random' -and
        $_.SuffixOrigin -ne 'WellKnown'
    })
# Prefer the stable "Link" address, then Dhcp/Manual, never Random.
$addr = $candidates | Sort-Object `
    @{ Expression = { if ($_.SuffixOrigin -eq 'Link') { 0 } else { 1 } } } |
    Select-Object -First 1
if (-not $addr) {
    Write-Host '[ddns] no stable global IPv6 found (LinkLayerAddress suffix)'
    exit 1
}
$ip = ($addr.IPAddress -split '%')[0]   # strip zone id if present

$query = "hostname=$([uri]::EscapeDataString($hostname))&token=$([uri]::EscapeDataString($token))&ipv6=$([uri]::EscapeDataString($ip))"
if (-not $NoIPv4) { $query += '&ipv4=auto' }   # A record for the IPv4-only mobile net

try {
    $resp = Invoke-WebRequest -Uri "https://dynv6.com/api/update?$query" `
        -UseBasicParsing -TimeoutSec 20
    $body = $resp.Content
    # dynv6 replies "ok" or "addresses updated" on success
    if ($resp.StatusCode -eq 200 -and $body -match 'ok|updated') {
        Write-Host "[ddns] $hostname -> $ip updated ($body)"
        exit 0
    }
    Write-Host "[ddns] unexpected reply: HTTP $($resp.StatusCode) $body"
    exit 1
} catch {
    Write-Host "[ddns] update failed: $($_.Exception.Message)"
    exit 1
}
