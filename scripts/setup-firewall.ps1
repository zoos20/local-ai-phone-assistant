#Requires -Version 5.1
<#
  One-time setup for the assistant server. Run PowerShell as Administrator.

  Adds the inbound firewall rules for the assistant API (port 8090, LAN)
  and the tunnel (port 8443, any address -- TLS + token protect it) so the
  phone can reach them. llama-server (8080) rules already exist.

  ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI, and
  non-ASCII characters in it break the parser.
#>

$ErrorActionPreference = 'Stop'

# Network profile must be Private for the LocalSubnet-restricted rules to apply.
foreach ($p in Get-NetConnectionProfile) {
    if ($p.NetworkCategory -ne 'Private') {
        Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private
        Write-Host "profile idx $($p.InterfaceIndex) -> Private" -ForegroundColor Yellow
    }
}

$remaining = Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -ne 'Private' }
$profiles = if ($remaining) { 'Private', 'Domain', 'Public' } else { 'Private', 'Domain' }

$port = 8090
$ruleName = "gemma-assistant $port (LAN)"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

New-NetFirewallRule -DisplayName $ruleName `
    -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $port `
    -Profile $profiles `
    -RemoteAddress LocalSubnet | Out-Null

$applied = (Get-NetFirewallRule -DisplayName $ruleName | Get-NetFirewallProfile |
            Where-Object Enabled | Select-Object -ExpandProperty Name) -join ', '
Write-Host "OK $port -> enabled on profiles: $applied" -ForegroundColor Green

# --- Tunnel port 8443 (wss) -----------------------------------------------
# Inbound from ANY address on all profiles: the channel is protected by TLS
# (self-signed cert, pinned on the phone) + ASSISTANT_API_TOKEN on open.
# The 8090 LAN rule above is left untouched -- the API itself stays LAN-only.
$tunnelPort = 8443
$tunnelName = "gemma-tunnel $tunnelPort (wss)"
Get-NetFirewallRule -DisplayName $tunnelName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $tunnelName `
    -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $tunnelPort `
    -Profile Any `
    -RemoteAddress Any | Out-Null
Write-Host "OK $tunnelPort -> inbound open on all profiles (TLS + token)" -ForegroundColor Green

# Verify from the LAN IP (loopback would not hit the rule at all).
$lan = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -like '192.168.*' } |
        Select-Object -First 1).IPAddress
Write-Host "Check from the phone: http://${lan}:8090/health" -ForegroundColor Cyan
Write-Host "Tunnel check (needs tunnel.py): https://${lan}:8443/health -k" -ForegroundColor Cyan
