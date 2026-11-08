# Removes Jarvis (installed by the Jarvis setup kit). Started by the "Uninstall Jarvis"
# shortcut in Desktop\JARVIS. Pictures, screenshots and files on the Desktop are kept.
# Keep this file ASCII-only (Windows PowerShell 5.1 reads it as ANSI).

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
$Host.UI.RawUI.WindowTitle = 'Uninstall Jarvis'

function Get-JarvisSetting([string] $name, [string] $default = '') {
    $f = Join-Path $Root 'JARVIS-SETTINGS.txt'
    if (Test-Path -LiteralPath $f) {
        foreach ($line in [IO.File]::ReadAllLines($f)) {
            $k, $v = $line -split '=', 2
            if ($null -ne $v -and -not $k.Trim().StartsWith('#') -and $k.Trim() -eq $name -and $v.Trim()) { return $v.Trim() }
        }
    }
    return $default
}
$Desktop = Get-JarvisSetting 'DESKTOP' ([Environment]::GetFolderPath('Desktop'))
$JarvisHome = Get-JarvisSetting 'JARVIS_HOME' (Join-Path $env:USERPROFILE '.openjarvis')

Write-Host ''
Write-Host "This removes Jarvis from this PC (the program in $Root)." -ForegroundColor Yellow
$ok = Read-Host 'Type YES and press Enter to uninstall, or just press Enter to cancel'
if ($ok -ne 'YES' -and $ok -ne 'yes') { Write-Host 'Cancelled. Nothing was changed.'; Start-Sleep -Seconds 3; exit 0 }

# 1. Stop Jarvis and everything it started.
$task = Get-ScheduledTask -TaskName 'OpenJarvis' -ErrorAction SilentlyContinue
if ($task -and ($task.Actions | Where-Object { $_.Arguments -like "*$Root\start-jarvis.ps1*" })) {
    Stop-ScheduledTask -TaskName 'OpenJarvis' -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName 'OpenJarvis' -Confirm:$false -ErrorAction SilentlyContinue
}
$prefix = $Root.TrimEnd('\') + '\'
Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, 'OrdinalIgnoreCase')) -or
    ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like "*$Root\start-jarvis.ps1*")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
Write-Host 'Jarvis stopped.'

# 2. Shortcuts, start-at-sign-in, notification name.
$folder = Join-Path $Desktop 'JARVIS'
foreach ($n in 'Jarvis.lnk', 'Restart Jarvis.lnk', 'Stop Jarvis.lnk', 'Save Google setting (copy it first).lnk',
               'Uninstall Jarvis.lnk', 'How to use Jarvis.txt') {
    Remove-Item (Join-Path $folder $n) -Force -ErrorAction SilentlyContinue
}
Remove-Item (Join-Path $Desktop 'Jarvis.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'Jarvis (starts in the background).lnk') -Force -ErrorAction SilentlyContinue
Remove-Item 'HKCU:\Software\Classes\AppUserModelId\OpenJarvis.Jarvis' -Recurse -Force -ErrorAction SilentlyContinue
if ((Test-Path $folder) -and -not (Get-ChildItem $folder -Force)) { Remove-Item $folder -Force }
Write-Host 'Shortcuts removed.'

# 3. Chats, memories and settings (asked separately).
if (Test-Path $JarvisHome) {
    $wipe = Read-Host "Also delete your Jarvis chats, memories and settings ($JarvisHome)? Type YES to delete, Enter to keep"
    if ($wipe -eq 'YES' -or $wipe -eq 'yes') { Remove-Item $JarvisHome -Recurse -Force -ErrorAction SilentlyContinue; Write-Host 'Deleted.' }
    else { Write-Host 'Kept.' }
}

# 4. The program folder (this script is already loaded, so it can delete itself).
Set-Location $env:TEMP
Remove-Item $Root -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path $Root) {
    Write-Host "Some files in $Root were in use; restart the PC and delete that folder by hand." -ForegroundColor Yellow
} else {
    Write-Host 'Jarvis has been removed.' -ForegroundColor Green
}
Write-Host 'Your pictures, screenshots and Desktop files were not touched.'
Read-Host 'Press Enter to close'
