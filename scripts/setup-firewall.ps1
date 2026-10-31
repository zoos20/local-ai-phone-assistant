#Requires -Version 5.1
<#
  One-time setup for the assistant server. Run PowerShell as Administrator.

  Adds the inbound firewall rule for the assistant API (port 8090) so the
  phone can reach it on the LAN. llama-server (8080) rules already exist.

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

# Verify from the LAN IP (loopback would not hit the rule at all).
$lan = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -like '192.168.*' } |
        Select-Object -First 1).IPAddress
Write-Host "Check from the phone: http://${lan}:8090/health" -ForegroundColor Cyan
