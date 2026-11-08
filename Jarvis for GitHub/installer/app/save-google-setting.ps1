# Saves what the user just COPIED into GOOGLE-ACCOUNT.txt, so they never have to
# edit the file by hand. Used by the "Save Google setting (copy it first)" shortcut
# in Desktop\JARVIS. It recognises what was copied:
#   - the calendar's secret iCal address (https://calendar.google.com/.../basic.ics)
#   - a Gmail app password (16 letters, spaces allowed)
#   - an email address
# Nothing secret is ever shown on screen. Keep this file ASCII-only.
# -Text / -File / -Test are only for testing; normally the clipboard is used.
param([string] $Text = $null,
      [string] $File = (Join-Path $PSScriptRoot 'GOOGLE-ACCOUNT.txt'),
      [switch] $Test)

function Say([string] $msg, [string] $icon = 'Information') {
    if ($Test) { "[$icon] $msg"; return }
    # 4096 = always on top (a plain message box could open hidden behind the browser);
    # 64 = information icon, 48 = warning icon.
    $flags = 4096 + $(if ($icon -eq 'Warning') { 48 } else { 64 })
    [void](New-Object -ComObject WScript.Shell).Popup($msg, 0, 'Jarvis - Google setup', $flags)
}

$Log = Join-Path $PSScriptRoot 'logs\google-setup.log'
function Note([string] $m) {   # what happened, never the copied text itself
    try { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $m" | Out-File -FilePath $Log -Append -Encoding ascii } catch { }
}

# (A [string] parameter is never $null - it defaults to '' - so check whether -Text
# was actually given. Checking $null here was why the shortcut ignored the clipboard.)
if (-not $PSBoundParameters.ContainsKey('Text')) {
    # Get-Clipboard came back empty when started from the hidden shortcut
    # (2026-11-07), so fall back to the .NET clipboard (needs -STA, set on the shortcut).
    $Text = ''
    try { $Text = Get-Clipboard -Raw } catch { }
    if (-not $Text) {
        try { Add-Type -AssemblyName System.Windows.Forms; $Text = [System.Windows.Forms.Clipboard]::GetText() } catch { }
    }
    Note ("clipboard read: " + $(if ($Text) { "$("$Text".Trim().Length) characters" } else { 'EMPTY' }) +
          ", apartment " + [Threading.Thread]::CurrentThread.GetApartmentState())
}
$t = "$Text".Trim().Trim('"', "'").Trim()

$key = $null
if ($t -match '^https://calendar\.google\.com/calendar/ical/\S+\.ics$') {
    $key = 'CALENDAR_ICAL_LINK'; $what = 'your calendar link'
} elseif ($t -match '^https://calendar\.google\.com/') {
    Say ("That's the normal calendar page link, not the secret one.`r`n`r`n" +
         "In Google Calendar: gear icon > Settings > click your name on the left > " +
         "'Integrate calendar' > 'Secret address in iCal format' > copy, then try again.") 'Warning'
    exit 1
} elseif ($t -match '^[A-Za-z]{4}( ?[A-Za-z]{4}){3}$') {
    $key = 'GMAIL_APP_PASSWORD'; $what = 'your Gmail app password'; $t = $t -replace ' ', ''
} elseif ($t -match '^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$') {
    $key = 'GMAIL_ADDRESS'; $what = "your Gmail address ($t)"
} else {
    Say ("I couldn't recognise what you copied.`r`n`r`n" +
         "Copy ONE of these first, then double-click this shortcut again:`r`n" +
         "  - the calendar's 'Secret address in iCal format'`r`n" +
         "  - the 16-letter Gmail app password`r`n" +
         "  - your Gmail address") 'Warning'
    exit 1
}

$lines = @()
if (Test-Path -LiteralPath $File) { $lines = @(Get-Content -LiteralPath $File) }
$found = $false
$lines = @($lines | ForEach-Object {
    if ($_ -match "^\s*$key\s*=") { $found = $true; "$key=$t" } else { $_ }
})
if (-not $found) { $lines += "$key=$t" }
[IO.File]::WriteAllLines($File, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
Note "saved $key"

# What's still missing?
$have = @{}
foreach ($l in $lines) { if ($l -match '^\s*([A-Z_]+)\s*=\s*(\S.*)$') { $have[$Matches[1]] = $true } }
$todo = @()
if (-not $have['GMAIL_ADDRESS']) { $todo += 'your Gmail address' }
if (-not $have['GMAIL_APP_PASSWORD']) { $todo += 'the Gmail app password' }
if (-not $have['CALENDAR_ICAL_LINK']) { $todo += 'the calendar secret address' }
$next = if ($todo.Count) { "`r`n`r`nStill to do (copy each, then double-click this shortcut): " + ($todo -join ', ') + '.' } else { "`r`n`r`nAll set! Ask Jarvis: 'Do I have new emails?' or 'What's on my calendar this week?'" }
Say ("Saved $what." + $next)
exit 0
