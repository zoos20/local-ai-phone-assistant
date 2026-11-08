#Requires -Version 5.1
<#
  Wireless debugging (Wi-Fi ADB) helper: connect / disconnect / status.

    powershell -File scripts\wireless-debug.ps1 -Action connect    -Ip <address>
    powershell -File scripts\wireless-debug.ps1 -Action disconnect -Ip <address>
    powershell -File scripts\wireless-debug.ps1 -Action status     [-Ip <address>]

  Actions:
    connect     adb connect <address>:5555, then re-reads "adb devices" and
                accepts ONLY the state "device" (offline/unauthorized fail
                with a hint). When the tcpip mode is off, prints the one-time
                USB instruction (adb tcpip 5555); tcpip is NEVER run by this
                script itself (it requires a USB cable).
    disconnect  closes port 5555 FIRST via "adb usb" through an available
                transport - over the live Wi-Fi session
                (adb -s <address>:5555 usb, USB cable NOT needed) or over
                the plugged cable (plain adb usb) - and only THEN drops the
                local TCP transport with "adb disconnect". The order matters:
                killing the transport first leaves nobody to deliver "adb usb"
                (cable out -> "no devices" -> port stays open). Everything is
                wrapped in try/finally (a port close is attempted even for an
                unusable -Ip and for a bare disconnect without -Ip);
                afterwards port 5555 is probed again (not for a bare
                disconnect).
    status      "adb devices" + Test-NetConnection <address> -Port 5555 with
                a plain-words verdict on why the phone is not reachable. A
                bad -Ip is a finding, not a failure: reported and exit 0.

  Exit codes: 0 = ok ("status" always exits 0; usage for connect; usage for
              disconnect when the fail-safe "adb usb" closed the port),
              1 = the action did not achieve its goal (including "adb usb"
              failing on a bare disconnect - the port may still be open),
              or adb is missing.

  Notes:
    - The PC side needs no firewall change: this is an outbound PC -> phone
      connection, so this script adds/edits no firewall rules at all.
    - Secrets (API token, certificate pin) are never read, stored or printed.
    - Huawei/EMUI resets the tcpip mode after a Wi-Fi reconnect or a reboot:
      repeat the one-time "adb tcpip 5555" over USB, then re-run connect.

  ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
  PowerShell 5.1, and non-ASCII characters in it break the parser.
#>
[CmdletBinding()]
param(
    [ValidateSet('connect', 'disconnect', 'status')]
    [string]$Action = 'status',

    # Phone address on the same Wi-Fi (IPv4 / host name / IPv6). The port is
    # fixed at 5555 and never has to be passed.
    [string]$Ip = ''
)

$ErrorActionPreference = 'Continue'   # adb's stderr must not become an exception
$Port = 5555

# --- locate adb: SDK platform-tools first (adb is often not in PATH) ----------
function Find-Adb {
    if ($env:LOCALAPPDATA) {
        $sdk = Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'
        if (Test-Path $sdk) { return $sdk }
    }
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd) {
        if ($cmd.Path) { return $cmd.Path }
        return $cmd.Definition
    }
    return $null
}

# --- run adb, capture stdout+stderr and the real exit code -------------------
function Invoke-Adb {
    param([Parameter(Mandatory = $true)][string[]]$AdbArgs)
    $lines = @(& $script:Adb @AdbArgs 2>&1 | ForEach-Object { $_.ToString() })
    [pscustomobject]@{
        Lines    = $lines
        ExitCode = $LASTEXITCODE
    }
}

# --- parse "adb devices" into objects (only well-known states) ---------------
function Get-AdbDevices {
    $res = Invoke-Adb -AdbArgs @('devices')
    $devices = @()
    foreach ($line in $res.Lines) {
        if ($line -match '^(?<serial>\S+)\s+(?<state>device|offline|unauthorized|recovery|sideload|bootloader)\b') {
            $devices += [pscustomobject]@{
                Serial = $Matches['serial']
                State  = $Matches['state']
            }
        }
    }
    [pscustomobject]@{ Raw = $res.Lines; Devices = $devices }
}

function Show-AdbRaw([object]$Info) {
    foreach ($line in $Info.Raw) {
        if ($line -ne '') { Write-Host "      $line" }
    }
}

# -Ip is an address only; the port is fixed at 5555. A stray ":5555" suffix
# is stripped (bracketed IPv6 too: "[fe80::1]:5555" -> fe80::1), an IPv6
# literal gets the brackets adb expects. Prints the reason and returns $null
# for a bad address - the CALLER decides what that means: connect/disconnect
# treat it as a failure (exit 1), status treats it as a diagnosis (exit 0).
function Resolve-Target([string]$Addr) {
    $a = $Addr.Trim()
    if ($a -match '://') {
        Write-Host "[wireless-debug] bad -Ip '$Addr': pass a bare address, no http:// and no port." -ForegroundColor Red
        return $null
    }
    # bracketed IPv6 with a stray port: [fe80::1]:5555 -> fe80::1
    if ($a -match '^\[(?<inner>[^\]]+)\]:\d+$') {
        $a = $Matches['inner']
    } elseif ($a -match '^\[(?<inner>[^\]]+)\]$') {
        # bracketed IPv6 without a port: [fe80::1] -> fe80::1
        $a = $Matches['inner']
    } elseif ($a -match '^(?<h>.+):\d+$' -and ($a -notmatch ':.*:')) {
        # stray ":5555" on IPv4/host only; two or more colons = IPv6, keep
        $a = $Matches['h']
    }
    if (-not $a) {
        Write-Host '[wireless-debug] bad -Ip: empty address.' -ForegroundColor Red
        return $null
    }
    $serial = if ($a -match ':') { "[$a]:$Port" } else { "${a}:$Port" }
    [pscustomobject]@{ Host = $a; Serial = $serial }
}

# Bounded TCP probe: $true only when something ACCEPTS the connection, i.e.
# a live listener = the ADB port is still open. Timeout and "connection
# refused" both count as "not open" (after disconnect the phone may be gone).
function Test-PortOpen([string]$Address, [int]$TimeoutMs) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($Address, $Port)
        if (-not $task.Wait($TimeoutMs)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Show-Usage([string]$Why) {
    Write-Host "[wireless-debug] $Why" -ForegroundColor Yellow
    Write-Host ''
    Write-Host 'Usage:'
    Write-Host '  powershell -File scripts\wireless-debug.ps1 -Action connect    -Ip <address>'
    Write-Host '  powershell -File scripts\wireless-debug.ps1 -Action disconnect -Ip <address>'
    Write-Host '  powershell -File scripts\wireless-debug.ps1 -Action status     [-Ip <address>]'
    Write-Host ''
    Write-Host '  <address>  phone IPv4/IPv6/host on the same Wi-Fi (port 5555 is fixed)'
    Write-Host '  status     without -Ip it still lists "adb devices"'
    Write-Host '  disconnect closes the port on the phone - run it when the session is done'
}

function Show-TcpipHint {
    Write-Host ''
    Write-Host '  Most likely the tcpip mode is OFF (or EMUI reset it):' -ForegroundColor Yellow
    Write-Host '    1. plug the phone into the PC over USB and accept the RSA dialog'
    Write-Host '    2. run once:  adb tcpip 5555'
    Write-Host '    3. unplug the cable and re-run this script with -Ip <address>'
    Write-Host '  This script never runs tcpip by itself - it needs the USB cable.'
    Write-Host '  Huawei/EMUI: the mode is lost after a Wi-Fi reconnect or a phone'
    Write-Host '  reboot - repeat step 2 over USB, that is normal for this phone.'
    Write-Host '  Also check the address: the phone IP changes on DHCP renewal'
    Write-Host '  (phone: Settings -> Wi-Fi -> current network -> IP).'
}

# --- locate adb (before any action) ------------------------------------------
$Adb = Find-Adb
if (-not $Adb) {
    Write-Host '[wireless-debug] adb NOT FOUND - cannot do anything.' -ForegroundColor Red
    Write-Host '  Install Android SDK platform-tools and use the path from README'
    Write-Host '  ("Build and install" section):'
    Write-Host '      %LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe'
    Write-Host '  or add any adb.exe directory to the PATH, then re-run.'
    exit 1
}

# ============================ connect =========================================
if ($Action -eq 'connect') {
    if (-not $Ip.Trim()) { Show-Usage 'connect requires -Ip <address>.'; exit 0 }
    $t = Resolve-Target $Ip
    if (-not $t) { exit 1 }   # bad -Ip already diagnosed above; connect fails

    Write-Host "[wireless-debug] adb connect $($t.Serial) ..." -ForegroundColor Cyan
    $cn = Invoke-Adb -AdbArgs @('connect', $t.Serial)
    foreach ($line in $cn.Lines) { if ($line -ne '') { Write-Host "    $line" } }
    if ($cn.ExitCode -ne 0) { Write-Host "    (adb connect exit code $($cn.ExitCode))" -ForegroundColor Yellow }

    $info = Get-AdbDevices
    $dev = $info.Devices | Where-Object { $_.Serial -eq $t.Serial } | Select-Object -First 1
    if (-not $dev -or $dev.State -eq 'offline') {
        # adbd often needs a moment to accept the socket.
        Start-Sleep -Milliseconds 1500
        $info = Get-AdbDevices
        $dev = $info.Devices | Where-Object { $_.Serial -eq $t.Serial } | Select-Object -First 1
    }

    if ($dev -and $dev.State -eq 'device') {
        Write-Host "[wireless-debug] CONNECTED: $($t.Serial) state=device" -ForegroundColor Green
        Write-Host '    The USB cable can be unplugged now. Useful commands:'
        Write-Host "      adb -s $($t.Serial) install -r -d <apk-path>"
        Write-Host "      adb -s $($t.Serial) logcat -v time"
        Write-Host '    Close the port when the session is done:'
        Write-Host '      scripts\wireless-debug.ps1 -Action disconnect -Ip <address>'
        exit 0
    }

    if ($dev) {
        Write-Host "[wireless-debug] FAILED: $($t.Serial) state=$($dev.State)" -ForegroundColor Red
        Write-Host '    adb devices:'
        Show-AdbRaw $info
        if ($dev.State -eq 'offline') {
            Write-Host '    The transport exists but is OFFLINE:' -ForegroundColor Red
            Write-Host '      - wait 2-3 seconds and re-run (the handshake may still be running);'
            Write-Host '      - if it stays offline: adb kill-server; adb start-server; re-run;'
            Write-Host '      - weak Wi-Fi or the phone changing IP mid-connect causes this too.'
        } elseif ($dev.State -eq 'unauthorized') {
            Write-Host '    The phone has not accepted this PC:' -ForegroundColor Red
            Write-Host '      - unlock the phone and accept the "Allow USB debugging?" dialog;'
            Write-Host '      - if it never appears: Developer options -> revoke USB debugging'
            Write-Host '        authorizations, then repeat once over USB.'
        } else {
            Write-Host "    Unexpected state '$($dev.State)' - see adb devices above." -ForegroundColor Red
        }
        exit 1
    }

    Write-Host "[wireless-debug] FAILED: $($t.Serial) is not in adb devices" -ForegroundColor Red
    Write-Host '    adb devices:'
    Show-AdbRaw $info
    Show-TcpipHint
    exit 1
}

# ============================ disconnect ======================================
# ORDER MATTERS: the port is closed FIRST ("adb usb" through an available
# transport - via the live Wi-Fi session when there is one, so the cable is
# not needed) and only THEN the local TCP transport is dropped with "adb
# disconnect". The other way round nobody is left to deliver "adb usb"
# (repro: cable out, session killed -> "no devices" -> port 5555 stayed
# open). Proof of this order: adb -s <ip>:5555 usb -> "restarting in USB
# mode" -> adbd restarts, the 5555 listener dies, the transport goes
# offline and is cleaned up by the disconnect below.
if ($Action -eq 'disconnect') {
    $hasIp = [bool]$Ip.Trim()
    if (-not $hasIp) { Show-Usage 'disconnect requires -Ip <address>.' }
    $t = $null
    $closeArgs = @('usb')     # plain "adb usb" unless the TCP transport wins
    $closeVia  = 'adb usb'
    $closeNote = $null
    $okClose = $false
    $okDisconnect = $false

    try {
        if (-not $hasIp) {
            # Bare "disconnect" (usage path): no address means no targeted
            # "adb disconnect", but exiting here used to leave port 5555 OPEN
            # while reporting success. Warn loudly and let the finally below
            # close the port - "adb usb" needs no address.
            Write-Host ''
            Write-Host "  WARNING: port $Port on the phone is NOT closed by this usage path" -ForegroundColor Yellow
            Write-Host '            - closing it now via adb usb (no -Ip needed)...'
        } else {
            # Validation lives INSIDE the try: whatever happens to -Ip, the
            # finally below still runs the port-closing command.
            $t = Resolve-Target $Ip   # prints the reason and yields $null on bad -Ip
            if (-not $t) {
                Write-Host '    adb disconnect SKIPPED: no usable -Ip (see the message above).' -ForegroundColor Yellow
                $closeNote = '    falling back to plain adb usb (any available transport).'
            } else {
                # Pick the transport BEFORE killing it: "adb usb" must travel
                # through something, and after "adb disconnect" there is
                # nothing left to send it to.
                $info = Get-AdbDevices
                $tcpDev = $info.Devices | Where-Object { $_.Serial -eq $t.Serial } | Select-Object -First 1
                $usbDev = $info.Devices | Where-Object { $_.Serial -notlike "*:$Port" } | Select-Object -First 1
                if ($tcpDev -and $tcpDev.State -eq 'device') {
                    # Live Wi-Fi session: adbd accepts "adb usb" over TCP and
                    # restarts itself in USB mode -> the 5555 listener dies.
                    $closeArgs = @('-s', $t.Serial, 'usb')
                    $closeVia  = "adb -s $($t.Serial) usb"
                    $closeNote = "    closing the port over the live Wi-Fi session ($closeVia) - USB cable NOT needed"
                } elseif ($usbDev) {
                    $closeNote = "    closing the port over the plugged USB cable ($closeVia)"
                } else {
                    $closeNote = "    no adb transport for $($t.Serial) (no session, no cable) - trying $closeVia anyway"
                }
            }
        }
    } finally {
        # Port-closing step: ALWAYS run it, even if the lines above threw or
        # the script was interrupted. It restarts adbd in USB mode on the
        # phone, which drops the TCP listener on 5555.
        if ($closeNote) { Write-Host $closeNote -ForegroundColor Cyan }
        Write-Host "[wireless-debug] closing port $Port on the phone ($closeVia) ..." -ForegroundColor Cyan
        $usb = Invoke-Adb -AdbArgs $closeArgs
        foreach ($line in $usb.Lines) { if ($line -ne '') { Write-Host "    $line" } }
        $okClose = ($usb.ExitCode -eq 0)

        # Local cleanup strictly AFTER the port close (see the block comment):
        # drops the - by then offline - TCP transport left by the restart.
        if ($t) {
            Write-Host "[wireless-debug] adb disconnect $($t.Serial) (detaching the local transport) ..." -ForegroundColor Cyan
            $dc = Invoke-Adb -AdbArgs @('disconnect', $t.Serial)
            foreach ($line in $dc.Lines) { if ($line -ne '') { Write-Host "    $line" } }
            $okDisconnect = ($dc.ExitCode -eq 0)
        }
    }

    if (-not $okClose) {
        Write-Host "[wireless-debug] WARNING: port-close command ($closeVia) did not run - port $Port may still be open." -ForegroundColor Yellow
        Write-Host '    Plug the phone over USB and run:  adb usb'
        Write-Host '    (or switch the phone Wi-Fi off/on to drop the listener).'
    }

    if (-not $hasIp) {
        # Bare usage path: the fail-safe above (finally) has been attempted.
        # No address is known here, so the port CANNOT be probed (it listens
        # on the phone) - exit code follows adb usb only, and the message
        # says exactly that instead of claiming an unverified closure.
        if ($okClose) {
            Write-Host "  adb usb done (port $Port closure not verified on this path; run status -Ip <ip> to confirm)." -ForegroundColor Green
            exit 0
        }
        exit 1
    }

    if (-not $t) {
        # Unusable -Ip: the targeted "adb disconnect" was skipped, but the
        # port-close command above (finally) was still run.
        Write-Host "[wireless-debug] FAILED: unusable -Ip; port-close command ($closeVia) was still run." -ForegroundColor Red
        exit 1
    }

    $info = Get-AdbDevices
    $rest = @($info.Devices | Where-Object { $_.Serial -like "*:$Port" })
    if ($rest.Count -gt 0) {
        Write-Host '    other TCP transports still open on this PC:' -ForegroundColor Yellow
        foreach ($d in $rest) { Write-Host "      $($d.Serial)  $($d.State)" }
    } else {
        Write-Host '    no TCP transports left on this PC'
    }

    # adbd restarts in USB mode and the phone drops the listener a moment
    # later - poll briefly instead of a single probe that could race it.
    $deadline = (Get-Date).AddSeconds(6)
    $open = Test-PortOpen $t.Host 3000
    while ($open -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 1000
        $open = Test-PortOpen $t.Host 3000
    }
    if ($open) {
        Write-Host "[wireless-debug] FAILED: port $Port on $($t.Host) is STILL OPEN" -ForegroundColor Red
        Write-Host '    Plug the phone over USB and run:  adb usb'
        exit 1
    }
    if ($okClose -or $okDisconnect) {
        Write-Host "[wireless-debug] DISCONNECTED: $($t.Serial); port $Port not listening (or phone unreachable)" -ForegroundColor Green
        Write-Host '    To reopen later:  scripts\wireless-debug.ps1 -Action connect -Ip <address>'
        exit 0
    }
    Write-Host '[wireless-debug] FAILED: neither the port-close command nor adb disconnect reached the phone.' -ForegroundColor Red
    exit 1
}

# ============================ status ==========================================
# Always exits 0: a missing phone is a diagnosis, not a script failure.
Write-Host '[wireless-debug] status' -ForegroundColor Cyan
Write-Host '    adb devices:'
$info = Get-AdbDevices
Show-AdbRaw $info

$devs = $info.Devices
if ($devs.Count -eq 0) {
    Write-Host '    no device right now (no USB cable, no live Wi-Fi session).' -ForegroundColor Yellow
} else {
    foreach ($d in $devs) {
        $kind = if ($d.Serial -like "*:$Port") { 'Wi-Fi' } else { 'USB' }
        Write-Host "    $($d.Serial)  state=$($d.State)  ($kind)" -ForegroundColor Green
    }
}

if (-not $Ip.Trim()) {
    Write-Host ''
    Write-Host '    Pass -Ip <address> to also probe TCP 5555 on the phone and get a verdict.'
    exit 0
}

$t = Resolve-Target $Ip
if (-not $t) {
    # A bad -Ip is a finding, not a failure: the adb devices output above is
    # the diagnosis and status keeps its "always exit 0" contract.
    Write-Host "    invalid -Ip '$Ip' (see the message above): pass a bare IPv4/host/IPv6," -ForegroundColor Yellow
    Write-Host '    no http://, the port 5555 is fixed. Nothing was probed.'
    Write-Host '    status treats this as a diagnosis, not a failure - exiting 0.'
    exit 0
}
Write-Host ''
Write-Host "    TCP probe $($t.Serial) (Test-NetConnection) ..." -ForegroundColor Cyan
$tn = Test-NetConnection -ComputerName $t.Host -Port $Port -WarningAction SilentlyContinue
Write-Host "      RemoteAddress        : $($tn.RemoteAddress)"
Write-Host "      TcpTestSucceeded     : $($tn.TcpTestSucceeded)"

$dev = $devs | Where-Object { $_.Serial -eq $t.Serial } | Select-Object -First 1
$usbDev = $devs | Where-Object { $_.Serial -notlike "*:$Port" } | Select-Object -First 1

if ($dev -and $dev.State -eq 'device' -and $tn.TcpTestSucceeded) {
    Write-Host "    VERDICT: connected over Wi-Fi ($($t.Serial) is 'device'), port $Port open." -ForegroundColor Green
} elseif ($dev -and $dev.State -eq 'device') {
    Write-Host "    VERDICT: adb has $($t.Serial) as 'device' but $t.Host does not answer port $Port." -ForegroundColor Yellow
    Write-Host '      Check the -Ip address (DHCP may have moved the phone to another IP).'
} elseif ($dev -and $dev.State -eq 'offline') {
    Write-Host "    VERDICT: $($t.Serial) is OFFLINE - adb sees it but the transport is dead." -ForegroundColor Red
    Write-Host '      Wait 2-3 s and re-run; if it persists: adb kill-server; adb start-server.'
} elseif ($tn.TcpTestSucceeded) {
    Write-Host "    VERDICT: port $Port on $($t.Host) is OPEN, but adb is not connected to it." -ForegroundColor Yellow
    Write-Host "      The tcpip mode is on - just run:  scripts\wireless-debug.ps1 -Action connect -Ip $($t.Host)"
} elseif ($usbDev) {
    Write-Host "    VERDICT: phone is visible over USB only ('$($usbDev.Serial)'), port $Port on $($t.Host) is closed." -ForegroundColor Yellow
    Show-TcpipHint
} else {
    Write-Host "    VERDICT: $($t.Host) does not answer port $Port and adb sees no device." -ForegroundColor Red
    Write-Host '      Why the connection fails (most common first):'
    Write-Host '      1. tcpip mode is OFF or EMUI reset it after a Wi-Fi reconnect/reboot'
    Write-Host '         -> one-time "adb tcpip 5555" over USB (see below);'
    Write-Host '      2. the phone is not on this Wi-Fi / not on the same network as the PC;'
    Write-Host '      3. the phone IP changed (DHCP) -> re-check it in phone Wi-Fi settings;'
    Write-Host '      4. wrong -Ip value (host name not resolvable from this PC).'
    Show-TcpipHint
}
exit 0
