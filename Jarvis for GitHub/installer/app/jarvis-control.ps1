# Helper behind the Desktop\JARVIS shortcuts.
#   jarvis-control.ps1 open     -> start Jarvis if needed, then open the web chat
#   jarvis-control.ps1 restart  -> stop Jarvis, start it again, open the web chat
#   jarvis-control.ps1 stop     -> stop Jarvis
# Keep this file ASCII-only (Windows PowerShell 5.1 reads it as ANSI).
param([ValidateSet('open', 'restart', 'stop')] [string] $Action = 'open')

$Root = $PSScriptRoot    # the Jarvis folder (this script lives in it)
# Values from JARVIS-SETTINGS.txt next to this script (lines like PORT=8000; # = comment).
function Get-JarvisSetting([string] $name, [string] $default = '') {
    $f = Join-Path $Root 'JARVIS-SETTINGS.txt'
    if (Test-Path -LiteralPath $f) {
        foreach ($line in [IO.File]::ReadAllLines($f)) {
            $k, $v = $line -split '=', 2
            if ($null -ne $v -and -not $k.Trim().StartsWith('#') -and $k.Trim() -eq $name) {
                if ($v.Trim()) { return $v.Trim() }
            }
        }
    }
    return $default
}
$Url = "http://127.0.0.1:$(Get-JarvisSetting 'PORT' '8000')"
$TaskName = 'OpenJarvis'
$StatusFile = Join-Path $Root 'logs\status.txt'

function Test-JarvisUp {
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Show-Message([string] $text) {
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($text, 'Jarvis', 'OK', 'Information')
}

function Stop-Jarvis {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $venv = Join-Path $Root 'src\.venv\'
    $node = Join-Path $Root 'tools\node\'
    $ollama = Join-Path $Root 'tools\ollama\'
    $popup = Join-Path $Root 'voice-popup\'
    $python = Join-Path $Root 'python\'      # base interpreter: voice pop-up, open-things add-on
    $imgTools = Join-Path $Root 'image-tools\'
    $gTools = Join-Path $Root 'google-tools\'
    Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and ($_.ExecutablePath.StartsWith($venv, 'OrdinalIgnoreCase') -or
                                 $_.ExecutablePath.StartsWith($node, 'OrdinalIgnoreCase') -or
                                 $_.ExecutablePath.StartsWith($ollama, 'OrdinalIgnoreCase') -or
                                 $_.ExecutablePath.StartsWith($popup, 'OrdinalIgnoreCase') -or
                                 $_.ExecutablePath.StartsWith($imgTools, 'OrdinalIgnoreCase') -or
                                 $_.ExecutablePath.StartsWith($gTools, 'OrdinalIgnoreCase') -or
                                 $_.ExecutablePath.StartsWith($python, 'OrdinalIgnoreCase'))) -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like '*-File*start-jarvis.ps1*')
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20 -and (Test-JarvisUp); $i++) { Start-Sleep -Milliseconds 500 }
}

function Start-Jarvis {
    if (Test-JarvisUp) { return $true }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Start-ScheduledTask -TaskName $TaskName
    } else {
        Start-Process -FilePath 'conhost.exe' -ArgumentList @('--headless', 'powershell.exe',
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$Root\start-jarvis.ps1`"")
    }
    # Startup takes 10-60 seconds; stop waiting early if the launcher gave up.
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep -Seconds 1
        if (Test-JarvisUp) { return $true }
        if ($i -gt 5 -and (Test-Path $StatusFile)) {
            $s = Get-Content $StatusFile -Raw
            if ($s -match 'NOT STARTED|STOPPED:') { return $false }
        }
    }
    return (Test-JarvisUp)
}

# Opens Jarvis in its own app window (Edge app mode: no tabs or address bar),
# or in the default browser if Edge is missing.
function Open-JarvisWindow {
    $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
              "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($edge) { Start-Process -FilePath $edge -ArgumentList "--app=$Url/" }
    else { Start-Process $Url }
}

if ($Action -eq 'stop') { Stop-Jarvis; Show-Message 'Jarvis has been stopped. Use the "Jarvis" shortcut to start it again.'; exit 0 }
if ($Action -eq 'restart') { Stop-Jarvis }

if (Start-Jarvis) {
    Open-JarvisWindow
} else {
    $why = if (Test-Path $StatusFile) { (Get-Content $StatusFile -Raw).Trim() } else { 'No status recorded.' }
    Show-Message "Jarvis could not start.`r`n`r`n$why`r`n`r`nLogs: $Root\logs"
}
