"""Dismiss only Chrome's leaked-password notice during a MIREA sign-in.

No keyboard events, focus changes, password changes, or global security settings.
The built-in Windows accessibility API invokes the notice's Close/OK button.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys

log = logging.getLogger(__name__)

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$scope = [System.Windows.Automation.TreeScope]::Descendants
$taskMatchAll = [System.Windows.Automation.Condition]::TrueCondition
$windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $taskMatchAll)
$closed = 0
foreach ($window in $windows) {
  if ($window.Current.ClassName -ne 'Chrome_WidgetWin_1') { continue }
  if ($window.Current.Name -notmatch 'МИРЭА|MIREA') { continue }
  $program = (Get-Process -Id $window.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName
  if ($program -notin @('chrome', 'msedge', 'browser', 'brave', 'opera')) { continue }
  $nodes = $window.FindAll($scope, $taskMatchAll)
  foreach ($node in $nodes) {
    if ($node.Current.Name -notmatch '^(Проверьте сохран[её]нные пароли|Смените пароль|Check your saved passwords|Change your password)$') { continue }
    $pane = $node
    for ($level = 0; $level -lt 5; $level++) {
      $pane = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($pane)
      if ($null -eq $pane -or $pane -eq $window) { break }
      $children = $pane.FindAll($scope, $taskMatchAll)
      $leakNotice = $false
      $button = $null
      foreach ($child in $children) {
        if ($child.Current.Name -match 'утечки данных|data breach') { $leakNotice = $true }
        if ($child.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
            $child.Current.Name -cmatch '^(Закрыть|ОК|OK|Close)$') { $button = $child }
      }
      if ($leakNotice -and $null -ne $button -and $button.Current.IsEnabled) {
        $pattern = $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke(); $closed++; break
      }
    }
  }
}
[Console]::Write($closed)
"""


def dismiss_password_notice() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        closed = (
            result.returncode == 0 and result.stdout.strip().isdigit() and int(result.stdout) > 0
        )
        if closed:
            log.info("browser_password_notice_closed")
        return closed
    except (OSError, subprocess.TimeoutExpired):
        log.debug("browser_password_notice_unavailable")
        return False
