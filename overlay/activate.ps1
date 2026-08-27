# Bring a session's window to the front by process id.
#
# Windows only lets the foreground process hand focus away, so a background
# helper (which is what the overlay spawns) is refused by both AppActivate and
# a bare SetForegroundWindow. Attaching to the current foreground thread's
# input queue puts this call inside that permission, which is the standard
# way to make focus transfer reliable.
param([int]$TargetPid)

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Fg {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint from, uint to, bool attach);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
"@

$proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
if (-not $proc) { exit 1 }
$target = $proc.MainWindowHandle
if ($target -eq [IntPtr]::Zero) { exit 1 }

if ([Fg]::IsIconic($target)) { [void][Fg]::ShowWindow($target, 9) }   # SW_RESTORE

$fg = [Fg]::GetForegroundWindow()
$fgPid = 0
$fgThread = [Fg]::GetWindowThreadProcessId($fg, [ref]$fgPid)
$me = [Fg]::GetCurrentThreadId()

$attached = $false
if ($fgThread -ne 0 -and $fgThread -ne $me) {
  $attached = [Fg]::AttachThreadInput($me, $fgThread, $true)
}
[void][Fg]::BringWindowToTop($target)
[void][Fg]::SetForegroundWindow($target)
if ($attached) { [void][Fg]::AttachThreadInput($me, $fgThread, $false) }
exit 0
