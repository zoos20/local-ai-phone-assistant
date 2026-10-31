#Requires -Version 5.1
<#
  One-time fix: make adb see the Huawei phone over USB, and open the
  assistant port in the firewall. Run PowerShell as Administrator.

  WHY: Windows bound the phone's ADB interface to plain WinUSB but did not
  register the ADB interface GUID, so adb.exe ignores the device. adb
  enumerates by GUID {F72FE0D4-CBCB-407D-8814-9ED673D0DD6B} -- we add it
  to Device Interface GUIDs and restart the device.

  ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI, and
  non-ASCII characters in it break the parser.
#>

$ErrorActionPreference = 'Stop'
$log = Join-Path $env:TEMP 'setup-adb.log'
try { Start-Transcript -Path $log -Force | Out-Null } catch {}

# --- 1/3  ADB interface GUID ------------------------------------------
Write-Host '1/3  Registering ADB interface GUID...' -ForegroundColor Cyan
$adbGuid = '{F72FE0D4-CBCB-407D-8814-9ED673D0DD6B}'
$done = 0
Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB' -ErrorAction SilentlyContinue |
    Where-Object { $_.PSChildName -like 'VID_12D1*' } |
    ForEach-Object {
        $inst = $_
        Get-ChildItem $inst.PSPath -ErrorAction SilentlyContinue | ForEach-Object {
            $friendly = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).FriendlyName
            if ($friendly -ne 'ADB Interface') { return }

            $params = Join-Path $_.PSPath 'Device Parameters'
            if (-not (Test-Path $params)) {
                New-Item $params -Force | Out-Null
            }
            $existing = (Get-ItemProperty -Path $params -Name 'DeviceInterfaceGUIDs' -ErrorAction SilentlyContinue).DeviceInterfaceGUIDs
            if ($existing -notcontains $adbGuid) {
                # REG_MULTI_SZ; New-ItemProperty understands PS provider paths
                $vals = [string[]](@($existing) + @($adbGuid) | Where-Object { $_ })
                New-ItemProperty -Path $params -Name 'DeviceInterfaceGUIDs' `
                    -PropertyType MultiString -Value $vals -Force | Out-Null
                Write-Host "    GUID added: $($_.PSChildName)" -ForegroundColor Yellow
            } else {
                Write-Host "    GUID already present: $($_.PSChildName)" -ForegroundColor DarkGray
            }

            # Restart the device so the new GUID takes effect.
            # pnputil needs the FULL instance id: USB\VID_...\Instance
            $fullId = "USB\$($inst.PSChildName)\$($_.PSChildName)"
            Write-Host "    restarting device $fullId ..." -ForegroundColor DarkGray
            pnputil /restart-device "$fullId" | Out-Null
            $done++
        }
    }
if ($done -eq 0) {
    Write-Host '    WARNING: no ADB Interface device found under VID_12D1.' -ForegroundColor Red
    Write-Host '    Check that USB debugging is ON and mode = File transfer.' -ForegroundColor Red
} else {
    Write-Host "    OK, $done device(s) updated" -ForegroundColor Green
}

# --- 2/3  Firewall rule for the assistant port -------------------------
Write-Host '2/3  Firewall rule for port 8090...' -ForegroundColor Cyan
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
Write-Host "    OK $port" -ForegroundColor Green

# --- 3/3  Network profile -> Private -----------------------------------
Write-Host '3/3  Network profile Private...' -ForegroundColor Cyan
foreach ($p in Get-NetConnectionProfile) {
    if ($p.NetworkCategory -ne 'Private') {
        Set-NetConnectionProfile -InterfaceIndex $p.InterfaceIndex -NetworkCategory Private
        Write-Host "    idx $($p.InterfaceIndex) -> Private" -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host 'DONE. Log: ' $log -ForegroundColor Green
Stop-Transcript | Out-Null
