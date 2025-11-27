#requires -Version 3.0
<#
.SYNOPSIS
    Simulates small mouse movements to maintain "Active" status in applications.
    
.DESCRIPTION
    Uses user32.dll (mouse_event) via P/Invoke to perform a 1-pixel relative mouse
    movement down and back up at a configurable interval. This resets typical
    application/OS idle timers (Teams, Slack, etc.) without materially moving
    the pointer.

.PARAMETER Minutes
    Total duration to run in minutes. Valid range: 1–10080. Default: 480 (8 hours).

.PARAMETER IntervalSeconds
    Interval between mouse jiggles in seconds. Valid range: 5–600. Default: 60.

.EXAMPLE
    .\Start-MouseJiggler.ps1 -Minutes 60 -IntervalSeconds 30 -Verbose

.NOTES
    Windows only. Requires PowerShell 3.0 or later.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateRange(1, 7*24*60)]
    [int]$Minutes = 480,

    [ValidateRange(5, 60*10)]
    [int]$IntervalSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This script works only on Microsoft Windows."
}

$csSource = @'
using System;
using System.Runtime.InteropServices;

namespace Win32
{
    public static class Win32Mouse
    {
        [DllImport("user32.dll", SetLastError = false)]
        public static extern void mouse_event(uint dwFlags, int dx, int dy, uint dwData, IntPtr dwExtraInfo);
    }
}
'@

try {
    $null = Add-Type -TypeDefinition $csSource -ErrorAction Stop
}
catch [System.Management.Automation.ExtendedTypeSystemException] {
}

[uint32]$MOUSEEVENTF_MOVE = 0x0001
$Win32Mouse = [Win32.Win32Mouse]

$duration = [TimeSpan]::FromMinutes($Minutes)
$endTime  = (Get-Date).Add($duration)

Write-Verbose ("Jiggler active for {0:N0} minutes, interval {1}s." -f $duration.TotalMinutes, $IntervalSeconds)
Write-Verbose "Press Ctrl+C to stop."

while (($now = Get-Date) -lt $endTime) {
    $Win32Mouse::mouse_event($MOUSEEVENTF_MOVE, 0, 1, 0, [IntPtr]::Zero)
    $Win32Mouse::mouse_event($MOUSEEVENTF_MOVE, 0, -1, 0, [IntPtr]::Zero)

    $remainingSeconds = [int][Math]::Ceiling(($endTime - $now).TotalSeconds)
    if ($remainingSeconds -le 0) { break }

    $sleepSeconds = [Math]::Min($IntervalSeconds, $remainingSeconds)
    for ($i = 0; $i -lt $sleepSeconds; $i++) {
        if ((Get-Date) -ge $endTime) { break }
        Start-Sleep -Seconds 1
    }
}

Write-Verbose "Timer expired. Jiggler stopped."



# Old version using SendKeys and NumLock toggle
# ---------------------------------------------
# param(
#     [int]
#     [ValidateRange(1, 7*24*60)]
#     $Minutes = 480
# )

# $ErrorActionPreference = 'Stop'

# Write-Host "Anti-idle script for Microsoft Windows."
# $response1 = Read-Host 'Would you like to proceed? [Y/n]'
# $response2 = $response1.Trim().ToLowerInvariant()
# if ($response1 -and -not $response2.StartsWith('y')) {
#     Write-Host '✕ Aborted by user.'
#     Write-Host ""
#     Write-Host ""
#     return
# }

# if ($env:OS -ne 'Windows_NT') {
#     throw "✕ Only Microsoft Windows is supported."
# }

# Add-Type -AssemblyName System.Windows.Forms
# Write-Host "✓ Anti-idle active for $Minutes minutes."
# Write-Host "  Press Ctrl+C to stop."

# $ToggleAction = {
#     [System.Windows.Forms.SendKeys]::SendWait("{NUMLOCK}")
#     [System.Windows.Forms.SendKeys]::SendWait("{NUMLOCK}")
# }

# for ($i = 1; $i -le $Minutes; $i++) {
#     Start-Sleep -Seconds 60
#     & $ToggleAction
# }