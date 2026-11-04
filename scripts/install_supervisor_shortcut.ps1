param(
    [Parameter(Mandatory=$true)][string]$RunDir,
    [Parameter(Mandatory=$true)][string]$Pythonw,
    [string]$Bootstrap,
    [switch]$Desktop
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'launch_supervisor.pyw'
$resolvedRun = (Resolve-Path -LiteralPath $RunDir).Path
$resolvedPython = (Resolve-Path -LiteralPath $Pythonw).Path
if ([IO.Path]::GetFileName($resolvedPython) -ne 'pythonw.exe') { throw 'Use the pinned pythonw.exe' }
$arguments = '"' + $launcher + '" --run-dir "' + $resolvedRun + '"'
if ($Bootstrap) { $arguments += ' --bootstrap "' + (Resolve-Path -LiteralPath $Bootstrap).Path + '"' }
$destinations = @((Join-Path $projectRoot 'Slack Codex Bridge.lnk'))
if ($Desktop) {
    $desktopFolder = [Environment]::GetFolderPath('Desktop')
    if (-not $desktopFolder) { throw 'Desktop folder is unavailable' }
    $destinations += (Join-Path $desktopFolder 'Slack Codex Bridge.lnk')
}
$shell = New-Object -ComObject WScript.Shell
foreach ($destination in $destinations) {
    $link = $shell.CreateShortcut($destination)
    if (Test-Path -LiteralPath $destination) {
        $legacy = '"' + (Join-Path $resolvedRun 'supervisor-launcher\launch.pyw') + '"'
        if ($link.TargetPath -ne $resolvedPython -or ($link.Arguments -ne $legacy -and $link.Arguments -ne $arguments)) {
            throw 'Existing shortcut belongs to another entry; inspect before replacement'
        }
    }
    $link.TargetPath = $resolvedPython
    $link.Arguments = $arguments
    $link.WorkingDirectory = $projectRoot
    $link.Description = 'Open Slack Codex Bridge supervisor'
    $link.IconLocation = $resolvedPython + ',0'
    $link.WindowStyle = 7
    $link.Save()
    $check = $shell.CreateShortcut($destination)
    if ($check.TargetPath -ne $resolvedPython -or $check.Arguments -ne $arguments) { throw 'Shortcut verification failed' }
}
@{verified=$true;shortcut_count=$destinations.Count} | ConvertTo-Json -Compress
