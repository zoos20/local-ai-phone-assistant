#Requires -Version 5.1
<#
    One-time network diagnostics for the hybrid tunnel (plan, phase 0).
    Run PowerShell as Administrator AFTER scripts\setup-firewall.ps1.

    What it does:
      1. shows public IPv4/IPv6 (ident.me) and the PC's global IPv6;
      2. starts a temporary listener on 8443 (dual-stack if possible) and
         prints external port-checker URLs -- open them from the PHONE's
         browser (mobile internet, Wi-Fi off) and confirm 8443 is reachable;
      3. reports what to fix in the router when it is not:
         - inbound IPv6 usually blocked by the router's "IPv6 firewall" /
           "network attack protection" -- disable it for this host;
         - IPv4 needs a white address or a port-forward TCP 8443 -> PC.
      4. prints the next steps: DDNS hostname in .env, ddns task, tunnel.

    Verdict:
      INBOUND IPv6 OK  -> main transport works (plan default).
      IPv4 only OK     -> set ASSISTANT_DDNS_HOSTNAME to a v4-capable name
                          or rely on the IPv4 fallback address manually.
      Nothing OK       -> the plan stops here: without Tailscale/VPS there
                          is no way in; revisit the transport choice.

    ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
    PowerShell 5.1, and non-ASCII characters in it break the parser.
#>

$ErrorActionPreference = 'Continue'
$Port = 8443

function Get-Public([string]$Family) {
    $url = if ($Family -eq '4') { 'https://v4.ident.me' } else { 'https://v6.ident.me' }
    try {
        $r = curl.exe -s --max-time 8 "-$Family" $url 2>$null
        if ($r) { return $r.Trim() }
    } catch { }
    return $null
}

Write-Host '=== 1. addresses ===' -ForegroundColor Cyan
$v4 = Get-Public 4
$v6 = Get-Public 6
Write-Host "public IPv4: $(if ($v4) { $v4 } else { 'NONE (no public v4 / curl failed)' })"
Write-Host "public IPv6: $(if ($v6) { $v6 } else { 'NONE (network has no IPv6 / curl failed)' })"

$global6 = Get-NetIPAddress -AddressFamily IPv6 -AddressState Preferred -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike 'fe80*' -and $_.SuffixOrigin -eq 'Link' } |
    Select-Object -First 1
Write-Host "stable global IPv6 (Link suffix): $(if ($global6) { $global6.IPAddress } else { 'NOT FOUND' })"

$rules = Get-NetFirewallRule -DisplayName "gemma-tunnel $Port*" -ErrorAction SilentlyContinue
if ($rules) {
    Write-Host "firewall rule for $Port : present" -ForegroundColor Green
} else {
    Write-Host "firewall rule for $Port : MISSING -- run scripts\setup-firewall.ps1" -ForegroundColor Red
}

Write-Host ''
Write-Host "=== 2. inbound test on $Port (temporary listener) ===" -ForegroundColor Cyan
$listener = $null
$bound = $null
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::IPv6Any, $Port)
    $listener.Server.DualMode = $true   # IPv4-mapped too
    $listener.Start()
    $bound = 'dual (IPv4 + IPv6)'
} catch {
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, $Port)
        $listener.Start()
        $bound = 'IPv4 only'
    } catch {
        Write-Host "cannot bind :$Port -- $($_.Exception.Message)" -ForegroundColor Red
        $listener = $null
    }
}

if ($listener) {
    Write-Host "listener bound: $bound"
    if ($v6) {
        Write-Host ''
        Write-Host 'Open these from the PHONE browser (mobile internet, Wi-Fi OFF):' -ForegroundColor Yellow
        Write-Host "  https://check-host.net/check-tcp?host=$([uri]::EscapeDataString("[$v6]${Port}"))"
        Write-Host '  https://portchecker.io        (hostname:' "$v6" "port: $Port)"
        Write-Host '  https://www.yougetsignal.com/tools/open-ports/'
    } else {
        Write-Host 'No public IPv6 -- external IPv6 check is impossible.' -ForegroundColor Yellow
    }
    if ($v4) {
        Write-Host "  IPv4 check: https://portchecker.io (hostname: $v4, port: $Port)"
        Write-Host '    (works only with a white IPv4 or a router port-forward to this PC)'
    }

    Write-Host ''
    Write-Host 'Press Enter here after the checks...' -ForegroundColor Yellow
    $deadline = (Get-Date).AddMinutes(5)
    $hits = 0
    while ((Get-Date) -lt $deadline) {
        try {
            while ($listener.Pending()) {
                $c = $listener.AcceptTcpClient()
                $hits++
                Write-Host "[diag] INBOUND #$hits from $($c.Client.RemoteEndPoint)" -ForegroundColor Green
                $c.Close()
            }
        } catch { }
        Start-Sleep -Milliseconds 250
        if ([Console]::KeyAvailable) {
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq 'Enter') { break }
        }
    }
    $listener.Stop()
    Write-Host ''
    if ($hits -gt 0) {
        Write-Host "VERDICT: inbound port $Port REACHABLE ($hits connections) -- tunnel can work" -ForegroundColor Green
    } else {
        Write-Host "VERDICT: no inbound connections seen on $Port" -ForegroundColor Red
        Write-Host '  IPv6: disable the router IPv6 firewall ("network attack protection")'
        Write-Host '        for this PC, then re-run this script.'
        Write-Host "  IPv4: add a port-forward TCP $Port -> this PC on the router."
    }
}

Write-Host ''
Write-Host '=== 3. next steps ===' -ForegroundColor Cyan
Write-Host '1. .env: set ASSISTANT_API_TOKEN (shared secret, both sides)'
Write-Host '2. .env: set ASSISTANT_DDNS_HOSTNAME=<your-zone>.dynv6.net + ASSISTANT_DDNS_TOKEN'
Write-Host '3. powershell -File scripts\setup-ddns-task.ps1      (AAAA refresh every 5 min)'
Write-Host '4. restart the stack (start-all.ps1 starts tunnel.py on 8443)'
Write-Host '5. phone: enter DDNS host + token in the app, press Start at home'
Write-Host '        once to pin the cert (TOFU via GET /cert over LAN)'
Write-Host ''
Write-Host 'Router port-forward template (IPv4 fallback):'
Write-Host "  external TCP $Port -> <PC LAN IPv4>:$Port"
