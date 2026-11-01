#Requires -Version 5.1
<#
    Registers the scheduled task "Gemma-DDNS": runs ddns-update.ps1 every
    5 minutes for the current user (no admin rights needed).

    Run once after filling ASSISTANT_DDNS_* in .env:
      powershell -ExecutionPolicy Bypass -File scripts\setup-ddns-task.ps1

    ASCII-only on purpose: a .ps1 saved without BOM is read as ANSI by
    PowerShell 5.1, and non-ASCII characters in it break the parser.
#>

$ErrorActionPreference = 'Stop'

$script = Join-Path $PSScriptRoot 'ddns-update.ps1'
if (-not (Test-Path $script)) { throw "not found: $script" }

$tr = "powershell.exe -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File `"$script`""
schtasks /Create /TN 'Gemma-DDNS' /SC MINUTE /MO 5 /TR $tr /F | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks failed with code $LASTEXITCODE" }

# Run it right away so the record is fresh without waiting 5 minutes.
schtasks /Run /TN 'Gemma-DDNS' | Out-Null
Write-Host "OK task 'Gemma-DDNS' registered (every 5 min) and triggered" -ForegroundColor Green
