# Bring a session's window to the front by process id.
# WScript.Shell's AppActivate fails silently against Windows' foreground lock,
# so restore the window and satisfy the lock before setting foreground.
param([int]$TargetPid)
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Fg {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern void SwitchToThisWindow(IntPtr h, bool alt);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@
$proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
if (-not $proc) { exit 1 }
$h = $proc.MainWindowHandle
if ($h -eq [IntPtr]::Zero) { exit 1 }
if ([Fg]::IsIconic($h)) { [void][Fg]::ShowWindow($h, 9) }   # SW_RESTORE
# a synthetic ALT tap releases the foreground lock for this call
[Fg]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero)
[Fg]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
[void][Fg]::SetForegroundWindow($h)
[Fg]::SwitchToThisWindow($h, $true)
