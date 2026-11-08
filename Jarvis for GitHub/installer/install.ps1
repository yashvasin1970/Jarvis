# Jarvis setup kit installer. Started by "Install Jarvis.cmd" (double-click).
# Installs Jarvis (OpenJarvis + voice bar + add-on tools) for the signed-in Windows user,
# with its own free NVIDIA key. Downloads about 3 GB (more with an NVIDIA graphics card).
# Keep this file ASCII-only (Windows PowerShell 5.1 reads it as ANSI).
#
# The parameters are only needed for testing; normally none are given.
param(
    [string] $InstallDir = (Join-Path $env:USERPROFILE 'OpenJarvis'),
    [string] $JarvisHome = '',        # settings folder; default <user>\.openjarvis
    [string] $DesktopDir = '',        # default: the real Desktop
    [int]    $Port = 8000,
    [string] $City = '',
    [ValidateSet('auto', 'cuda', 'cpu')] [string] $Gpu = 'auto',
    [string] $UvCacheDir = '',        # testing: reuse an existing download cache
    [switch] $NoAutoStart,            # testing: don't start Jarvis at sign-in
    [switch] $NoStart,                # testing: don't start Jarvis at the end
    [switch] $Unattended              # testing: never wait for the user
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# ---- Versions (the ones running on the PC this kit was made on) -------------------------
$UvVersion     = '0.13.7'
$PythonVersion = '3.12.16'
$NodeVersion   = '26.13.0'
$McpFsVersion  = '2.1.0'           # @modelcontextprotocol/server-filesystem
$SpacyWheelUrl = 'https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl'
$CublasVersion = '12.9.2.10'       # nvidia-cublas-cu12, for GPU listening
$TorchFlavor   = 'cu130'           # GPU PyTorch build (needs an NVIDIA driver with CUDA 13)

$Kit  = $PSScriptRoot              # ...\Jarvis Setup Kit\installer
$Root = [IO.Path]::GetFullPath($InstallDir)
if (-not $JarvisHome) { $JarvisHome = Join-Path $env:USERPROFILE '.openjarvis' }
$CustomDesktop = [bool]$DesktopDir
if (-not $DesktopDir) { $DesktopDir = [Environment]::GetFolderPath('Desktop') }
$JarvisFolder = Join-Path $DesktopDir 'JARVIS'
$Url = "http://127.0.0.1:$Port"
$Log = Join-Path $Root 'logs\install.log'
$Steps = 11

# ---- Helpers ----------------------------------------------------------------------------
function Write-Log([string] $text) {
    try { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $text" | Out-File -FilePath $Log -Append -Encoding utf8 } catch { }
}
function Say([string] $text, [string] $color = 'Gray') { Write-Host $text -ForegroundColor $color; Write-Log $text }
function Step([int] $n, [string] $text) { Write-Host ''; Say "[$n/$Steps] $text" 'Cyan' }
function Fail([string] $text) {
    Write-Host ''
    Say "SETUP STOPPED: $text" 'Red'
    Say "Details are in $Log" 'Red'
    Say 'You can run "Install Jarvis" again; it carries on from where it stopped.' 'Yellow'
    exit 1
}
function Ask([string] $question) {
    if ($Unattended) { return '' }
    return (Read-Host $question)
}

# Runs a program, logs its output, stops the setup if it fails.
function Run([string] $what, [string] $exe, [string[]] $argList, [switch] $NoFail) {
    Write-Log "RUN $what : $exe $($argList -join ' ')"
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $out = & $exe @argList 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($out) { ($out -join "`r`n") | Out-File -FilePath $Log -Append -Encoding utf8 }
    if ($code -ne 0 -and -not $NoFail) { Fail "$what didn't work (exit code $code)." }
    return $code
}

# Downloads a file (curl.exe is built into Windows 10/11; fallback: PowerShell).
function Get-File([string] $what, [string] $url, [string] $dest) {
    Write-Log "DOWNLOAD $what : $url"
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    for ($try = 1; $try -le 3; $try++) {
        try {
            if (Test-Path $curl) {
                $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                & $curl -L --fail -s -S --retry 3 --retry-delay 5 -o $dest $url 2>&1 | Out-File -FilePath $Log -Append -Encoding utf8
                $code = $LASTEXITCODE; $ErrorActionPreference = $old
                if ($code -eq 0 -and (Test-Path $dest)) { return }
            } else {
                Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
                return
            }
        } catch { Write-Log "download attempt $try failed: $_" }
        Start-Sleep -Seconds (5 * $try)
    }
    Fail "Couldn't download $what. Check the internet connection and try again."
}

# Runs a program just to read what it prints (never stops the setup).
function Get-Output([string] $exe, [string[]] $argList) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $o = & $exe @argList 2>$null | Out-String } catch { $o = '' }
    $ErrorActionPreference = $old
    return "$o".Trim()
}

# Unzips with Windows' built-in tar (fast), else PowerShell.
function Expand-Zip([string] $zip, [string] $dest) {
    New-Item -ItemType Directory -Force $dest | Out-Null
    $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (Test-Path $tar) { [void](Run "Unpacking $(Split-Path $zip -Leaf)" $tar @('-xf', $zip, '-C', $dest)) }
    else { Expand-Archive -Path $zip -DestinationPath $dest -Force }
}

# Python environments are made with Python's own venv module, NOT `uv venv`: uv writes a
# python.exe launcher containing the folder path, so it's different on every PC, and Windows
# Smart App Control blocks such never-seen-before programs. The venv module's launcher is the
# same file everywhere, so Windows recognises it. (Re-made if an old one can't run.)
function New-Venv([string] $venv) {
    $py = Join-Path $venv 'Scripts\python.exe'
    if ((Test-Path $py) -and ((Get-Output $py @('-c', 'print(6*7)')) -eq '42')) { return }
    if (Test-Path $venv) { Remove-Item $venv -Recurse -Force }
    [void](Run "Creating a Python environment ($(Split-Path (Split-Path $venv) -Leaf))" $BasePy @('-m', 'venv', '--without-pip', $venv))
    if ((Get-Output $py @('-c', 'print(6*7)')) -ne '42') { Fail "A new Python environment in $venv can't run (blocked by Windows?)." }
}

function Test-JarvisUp {
    try { return ((Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) }
    catch { return $false }
}

function Get-KeyOk {
    $f = Join-Path $Root 'NVIDIA-API-KEY.txt'
    if (-not (Test-Path $f)) { return $false }
    $raw = Get-Content -LiteralPath $f -Raw
    if (-not $raw) { return $false }
    $k = "$raw".Trim().Trim('"', "'").Trim() -replace '^(NIM_API_KEY|NVIDIA_API_KEY)\s*=\s*', ''
    return $k.Trim().Trim('"', "'").StartsWith('nvapi-')
}

# Stops a Jarvis that is already installed in this folder (before updating it).
function Stop-ExistingJarvis {
    $task = Get-ScheduledTask -TaskName 'OpenJarvis' -ErrorAction SilentlyContinue
    if ($task -and ($task.Actions | Where-Object { $_.Arguments -like "*$Root\start-jarvis.ps1*" })) {
        Stop-ScheduledTask -TaskName 'OpenJarvis' -ErrorAction SilentlyContinue
    }
    $prefix = $Root.TrimEnd('\') + '\'
    Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, 'OrdinalIgnoreCase')) -or
        ($_.Name -eq 'powershell.exe' -and $_.CommandLine -like "*$Root\start-jarvis.ps1*")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

# ---- Start -----------------------------------------------------------------------------
New-Item -ItemType Directory -Force (Join-Path $Root 'logs') | Out-Null
$Host.UI.RawUI.WindowTitle = 'Jarvis setup'
Write-Host ''
Write-Host '  ============================================' -ForegroundColor Magenta
Write-Host '                 JARVIS  SETUP' -ForegroundColor Magenta
Write-Host '  ============================================' -ForegroundColor Magenta
Say "Jarvis will be installed in: $Root"
Say 'This takes about 15-40 minutes (it downloads about 3 GB). Keep this window open'
Say 'and the PC switched on and connected to the internet.'
Write-Log "Kit: $Kit | settings: $JarvisHome | Desktop: $DesktopDir | port $Port | gpu $Gpu"

if (-not [Environment]::Is64BitOperatingSystem) { Fail 'Jarvis needs 64-bit Windows 10 or 11.' }
foreach ($need in @('source\jarvis-source.zip', 'app\start-jarvis.ps1', 'config-template.toml')) {
    if (-not (Test-Path (Join-Path $Kit $need))) {
        Fail 'Some setup files are missing. Unzip the whole kit first (right-click the zip > Extract All), then run "Install Jarvis" from the unzipped folder.'
    }
}

# ---- 1. Checks --------------------------------------------------------------------------
Step 1 'Checking this PC'
$drive = Get-PSDrive -Name ($Root.Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive -and $drive.Free -lt 12GB) {
    Fail ("Not enough free disk space: Jarvis needs about 12 GB, and drive {0}: has {1:N1} GB free." -f $drive.Name, ($drive.Free / 1GB))
}
try { [void](Invoke-WebRequest -Uri 'https://github.com' -UseBasicParsing -TimeoutSec 20 -Method Head) }
catch { Fail "This PC can't reach the internet (github.com). Connect and try again." }

$cudaMajor = 0
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $smiOut = Get-Output $smi.Source @()
    # Older drivers print "CUDA Version: 13.0", newer ones "CUDA UMD Version: 13.3".
    if ($smiOut -match 'CUDA (?:UMD )?Version:\s*(\d+)\.') { $cudaMajor = [int]$Matches[1] }
}
$UseGpu = ($Gpu -eq 'cuda') -or ($Gpu -eq 'auto' -and $cudaMajor -ge 13)
if ($UseGpu) { Say 'NVIDIA graphics card found: voice will run on it (faster). This adds about 3 GB to the download.' 'Green' }
elseif ($smi) { Say 'NVIDIA graphics card found, but its driver is too old for the GPU voice; using the processor instead. (Update the NVIDIA driver and run setup again to use it.)' 'Yellow' }
else { Say 'No NVIDIA graphics card: voice will run on the processor (a little slower, works fine).' }

if (Test-Path (Join-Path $Root 'start-jarvis.ps1')) {
    Say 'Jarvis is already installed here. It will be updated; your key, settings, chats and memories are kept.' 'Yellow'
    Stop-ExistingJarvis
}
if (Test-JarvisUp) { Fail "Something else is already using $Url. Stop it (or the other Jarvis) and run setup again." }

if (-not $City) {
    $City = Ask 'Which city do you live in? (used for weather; press Enter to skip)'
}
$City = ("$City" -replace '[=\r\n#]', ' ').Trim()

# ---- 2. Package manager (uv) ------------------------------------------------------------
Step 2 'Getting the installer tools'
foreach ($d in 'tools', 'cache\wheels', 'cache\downloads', 'data', 'models') { New-Item -ItemType Directory -Force (Join-Path $Root $d) | Out-Null }
$Uv = Join-Path $Root 'tools\uv.exe'
$uvOk = (Test-Path $Uv) -and ((Get-Output $Uv @('--version')) -like "uv $UvVersion*")
if (-not $uvOk) {
    $zip = Join-Path $Root 'cache\downloads\uv.zip'
    Get-File 'uv' "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" $zip
    $tmp = Join-Path $Root 'cache\downloads\uv'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Zip $zip $tmp
    Get-ChildItem $tmp -Recurse -Filter 'uv*.exe' | ForEach-Object { Copy-Item $_.FullName (Join-Path $Root 'tools') -Force }
}
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'python'
$env:UV_CACHE_DIR          = $(if ($UvCacheDir) { $UvCacheDir } else { Join-Path $Root 'cache\uv' })
$env:UV_PYTHON_PREFERENCE  = 'only-managed'
$env:UV_HTTP_TIMEOUT       = '600'
$env:UV_NO_PROGRESS        = '1'
$env:SETUPTOOLS_SCM_PRETEND_VERSION = '1.0.5'   # the copy has no git history to read a version from

# ---- 3. Python --------------------------------------------------------------------------
Step 3 "Installing Python $PythonVersion (private copy, only for Jarvis)"
[void](Run 'Installing Python' $Uv @('python', 'install', $PythonVersion))
$BasePy = (Get-Output $Uv @('python', 'find', $PythonVersion)) -split "`r?`n" | Select-Object -First 1
if (-not $BasePy -or -not (Test-Path $BasePy)) { Fail 'Python was installed but could not be found.' }
$BasePy = "$BasePy".Trim()
Write-Log "Python: $BasePy"

# ---- 4. Jarvis program ------------------------------------------------------------------
Step 4 'Unpacking the Jarvis program'
$Src = Join-Path $Root 'src'
New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Zip (Join-Path $Kit 'source\jarvis-source.zip') $Src
Copy-Item (Join-Path $Kit 'wheels\*.whl') (Join-Path $Root 'cache\wheels') -Force

# ---- 5. Jarvis's packages (the big download) --------------------------------------------
Step 5 "Downloading Jarvis's parts (about 2-3 GB; this is the longest step)"
$MainPy = Join-Path $Src '.venv\Scripts\python.exe'
New-Venv (Join-Path $Src '.venv')
Push-Location $Src
[void](Run "Installing Jarvis's packages" $Uv @('sync', '--frozen', '--inexact', '--python', $BasePy,
    '--extra', 'desktop', '--extra', 'pdf', '--extra', 'voice'))
Pop-Location
$spacy = Join-Path $Root 'cache\wheels\en_core_web_sm-3.8.0-py3-none-any.whl'
if (-not (Test-Path $spacy)) { Get-File 'the English language model' $SpacyWheelUrl $spacy }
$rust = Get-ChildItem (Join-Path $Root 'cache\wheels') -Filter 'openjarvis_rust-*.whl' | Select-Object -First 1
[void](Run 'Installing the ready-built parts' $Uv @('pip', 'install', '--offline', '--no-deps', '--python', $MainPy,
    $rust.FullName, $spacy))

# ---- 6. Graphics card voice (only with a suitable NVIDIA card) ----------------------------
Step 6 $(if ($UseGpu) { 'Setting up the graphics card for voice (about 3 GB)' } else { 'Graphics card voice: not needed on this PC' })
if ($UseGpu) {
    $probe = Join-Path $Root 'cache\downloads\torch-version.py'
    'import importlib.metadata as m; print(m.version("torch").split("+")[0])' | Out-File -FilePath $probe -Encoding ascii
    $tv = Get-Output $MainPy @('-I', $probe)
    if ($tv) {
        [void](Run 'GPU PyTorch' $Uv @('pip', 'install', '--python', $MainPy, '--no-deps', '--reinstall-package', 'torch',
            '--index-url', "https://download.pytorch.org/whl/$TorchFlavor", "torch==$tv+$TorchFlavor"))
    }
    $cublas = Get-ChildItem (Join-Path $Root 'cache\wheels') -Filter "nvidia_cublas_cu12-$CublasVersion-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cublas) {
        $info = Invoke-RestMethod -Uri "https://pypi.org/pypi/nvidia-cublas-cu12/$CublasVersion/json" -UseBasicParsing
        $file = $info.urls | Where-Object { $_.filename -like '*win_amd64.whl' } | Select-Object -First 1
        if (-not $file) { Fail 'The NVIDIA cuBLAS download could not be found.' }
        Get-File 'NVIDIA cuBLAS' $file.url (Join-Path $Root "cache\wheels\$($file.filename)")
        $cublas = Get-Item (Join-Path $Root "cache\wheels\$($file.filename)")
    }
    [void](Run 'NVIDIA cuBLAS' $Uv @('pip', 'install', '--offline', '--no-deps', '--python', $MainPy, $cublas.FullName))
}

# ---- 7. Desktop file access (Node.js + the official MCP filesystem server) ---------------
Step 7 'Setting up Desktop file access'
$Node = Join-Path $Root 'tools\node\node.exe'
$nodeOk = (Test-Path $Node) -and ((Get-Output $Node @('--version')) -eq "v$NodeVersion")
if (-not $nodeOk) {
    $zip = Join-Path $Root 'cache\downloads\node.zip'
    Get-File 'Node.js' "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip" $zip
    $tmp = Join-Path $Root 'cache\downloads\node'
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Zip $zip $tmp
    if (Test-Path (Join-Path $Root 'tools\node')) { Remove-Item (Join-Path $Root 'tools\node') -Recurse -Force }
    Move-Item (Join-Path $tmp "node-v$NodeVersion-win-x64") (Join-Path $Root 'tools\node')
}
$fs = Join-Path $Root 'tools\mcp-filesystem'
New-Item -ItemType Directory -Force $fs | Out-Null
"{`"name`":`"jarvis-mcp-filesystem`",`"private`":true,`"dependencies`":{`"@modelcontextprotocol/server-filesystem`":`"$McpFsVersion`"}}" |
    Out-File -FilePath (Join-Path $fs 'package.json') -Encoding ascii
$env:Path = (Join-Path $Root 'tools\node') + ';' + $env:Path
Push-Location $fs
[void](Run 'Desktop file access' (Join-Path $Root 'tools\node\npm.cmd') @('install', '--no-audit', '--no-fund', '--omit=dev',
    '--cache', (Join-Path $Root 'cache\npm')))
Pop-Location

# ---- 8. Add-on tools and the voice bar ---------------------------------------------------
Step 8 'Setting up the extra tools and the "Hey Jarvis" voice bar'
foreach ($name in 'image-tools', 'google-tools', 'voice-popup') {
    $dir = Join-Path $Root $name
    New-Item -ItemType Directory -Force $dir | Out-Null
    New-Venv (Join-Path $dir '.venv')
    [void](Run "$name packages" $Uv @('pip', 'install', '--python', (Join-Path $dir '.venv\Scripts\python.exe'),
        '-r', (Join-Path $Kit "requirements\$name.txt")))
}
$dl = Join-Path $Root 'cache\downloads\wake-models.py'
@('import openwakeword.utils as u', 'u.download_models(["hey_jarvis"])') | Out-File -FilePath $dl -Encoding ascii
[void](Run 'Wake word models' (Join-Path $Root 'voice-popup\.venv\Scripts\python.exe') @('-I', $dl))

# ---- 9. Jarvis's own files, settings and personality --------------------------------------
Step 9 'Writing settings'
# File by file (Copy-Item -Recurse nests folders that already exist when updating).
$appDir = Join-Path $Kit 'app'
Get-ChildItem $appDir -Recurse -File | ForEach-Object {
    $dest = Join-Path $Root $_.FullName.Substring($appDir.Length + 1)
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Copy-Item $_.FullName $dest -Force
}
Copy-Item (Join-Path $Kit 'uninstall.ps1') $Root -Force
Get-ChildItem $Root -Recurse -File -Include *.ps1, *.py -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

$settings = Join-Path $Root 'JARVIS-SETTINGS.txt'
if (-not (Test-Path $settings)) {
    $lines = @(
        '# Jarvis settings. Change a value, save this file, then use the "Restart Jarvis" shortcut.',
        '#',
        '# CITY  - your home city for weather and the morning briefing.',
        '# PORT  - where the Jarvis web chat runs on this PC (http://127.0.0.1:PORT). Leave it at 8000.',
        '# GPU   - cuda = use the NVIDIA graphics card for voice; cpu = don''t.',
        "CITY=$City",
        "PORT=$Port",
        "GPU=$(if ($UseGpu) { 'cuda' } else { 'cpu' })")
    if ($CustomDesktop) { $lines += "DESKTOP=$DesktopDir" }
    if ($JarvisHome -ne (Join-Path $env:USERPROFILE '.openjarvis')) { $lines += "JARVIS_HOME=$JarvisHome" }
    [IO.File]::WriteAllLines($settings, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
}
$keyFile = Join-Path $Root 'NVIDIA-API-KEY.txt'
if (-not (Test-Path $keyFile)) { New-Item -ItemType File $keyFile | Out-Null }

New-Item -ItemType Directory -Force $JarvisHome | Out-Null
$config = Join-Path $JarvisHome 'config.toml'
if (Test-Path $config) { Copy-Item $config "$config.before-setup-$(Get-Date -Format 'yyyyMMdd-HHmmss')" }
function J([string] $s) { return (ConvertTo-Json $s) }   # quoted + escaped for JSON / TOML
$toml = Get-Content (Join-Path $Kit 'config-template.toml') -Raw
$map = @{
    '{{DATE}}' = (Get-Date -Format 'yyyy-MM-dd'); '{{ROOT}}' = $Root; '{{PORT}}' = "$Port"
    '{{SPEECH_DEVICE}}' = $(if ($UseGpu) { 'cuda' } else { 'cpu' })
    '{{COMPUTE_TYPE}}'  = $(if ($UseGpu) { 'float16' } else { 'int8' })
    '{{J_NODE}}'     = (J (Join-Path $Root 'tools\node\node.exe'))
    '{{J_MCPFS}}'    = (J (Join-Path $fs 'node_modules\@modelcontextprotocol\server-filesystem\dist\index.js'))
    '{{J_DESKTOP}}'  = (J $DesktopDir)
    '{{J_BASEPY}}'   = (J $BasePy)
    '{{J_PCTOOLS}}'  = (J (Join-Path $Root 'pc-tools\pc_tools_server.py'))
    '{{J_IMGPY}}'    = (J (Join-Path $Root 'image-tools\.venv\Scripts\python.exe'))
    '{{J_IMGTOOLS}}' = (J (Join-Path $Root 'image-tools\image_tools_server.py'))
    '{{J_GPY}}'      = (J (Join-Path $Root 'google-tools\.venv\Scripts\python.exe'))
    '{{J_GTOOLS}}'   = (J (Join-Path $Root 'google-tools\google_tools_server.py'))
}
foreach ($k in $map.Keys) { $toml = $toml.Replace($k, $map[$k]) }
[IO.File]::WriteAllText($config, $toml, (New-Object Text.UTF8Encoding $false))   # TOML: no BOM
if (-not (Test-Path (Join-Path $JarvisHome 'SOUL.md'))) { Copy-Item (Join-Path $Kit 'home\SOUL.md') $JarvisHome }

# ---- 10. Shortcuts, guide and starting at sign-in -----------------------------------------
Step 10 'Creating the Desktop shortcuts'
New-Item -ItemType Directory -Force $JarvisFolder | Out-Null
$shell = New-Object -ComObject WScript.Shell
$conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
$icon = Join-Path $Root 'jarvis-round.ico'
function New-Shortcut([string] $path, [string] $target, [string] $arguments, [int] $style) {
    $l = $shell.CreateShortcut($path)
    $l.TargetPath = $target; $l.Arguments = $arguments; $l.WorkingDirectory = $Root
    $l.WindowStyle = $style; $l.IconLocation = "$icon,0"; $l.Save()
}
$ps = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File"
New-Shortcut (Join-Path $JarvisFolder 'Jarvis.lnk') $conhost "--headless $ps `"$Root\jarvis-control.ps1`" open" 7
New-Shortcut (Join-Path $JarvisFolder 'Restart Jarvis.lnk') $conhost "--headless $ps `"$Root\jarvis-control.ps1`" restart" 7
New-Shortcut (Join-Path $JarvisFolder 'Stop Jarvis.lnk') $conhost "--headless $ps `"$Root\jarvis-control.ps1`" stop" 7
New-Shortcut (Join-Path $JarvisFolder 'Save Google setting (copy it first).lnk') $conhost `
    "--headless powershell.exe -STA -NoProfile -ExecutionPolicy Bypass -File `"$Root\save-google-setting.ps1`"" 7
New-Shortcut (Join-Path $JarvisFolder 'Uninstall Jarvis.lnk') (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
    "-NoProfile -ExecutionPolicy Bypass -File `"$Root\uninstall.ps1`"" 1
New-Shortcut (Join-Path $DesktopDir 'Jarvis.lnk') $conhost "--headless $ps `"$Root\jarvis-control.ps1`" open" 7

$guide = Get-Content (Join-Path $Kit 'guide-template.txt') -Raw
$guide = $guide.Replace('{{ROOT}}', $Root).Replace('{{HOME}}', $JarvisHome).Replace('{{PORT}}', "$Port").
    Replace('{{CITY}}', $(if ($City) { $City } else { 'your city' }))
[IO.File]::WriteAllText((Join-Path $JarvisFolder 'How to use Jarvis.txt'), $guide, (New-Object Text.UTF8Encoding $true))

$AutoStart = 'none'
if (-not $NoAutoStart) {
    try {
        $me = "$env:USERDOMAIN\$env:USERNAME"
        $action = New-ScheduledTaskAction -Execute $conhost -WorkingDirectory $Root `
            -Argument "--headless $ps `"$Root\start-jarvis.ps1`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
        $trigger.Delay = 'PT20S'
        $principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
        $set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName 'OpenJarvis' -Action $action -Trigger $trigger -Principal $principal `
            -Settings $set -Description 'Starts Jarvis in the background when you sign in.' -Force | Out-Null
        $AutoStart = 'task'
        Say 'Jarvis will start by itself whenever you sign in to Windows.'
    } catch {
        Write-Log "Scheduled task failed: $_ ; using the Startup folder instead"
        $startup = [Environment]::GetFolderPath('Startup')
        New-Shortcut (Join-Path $startup 'Jarvis (starts in the background).lnk') $conhost "--headless $ps `"$Root\start-jarvis.ps1`"" 7
        $AutoStart = 'startup'
        Say 'Jarvis will start by itself whenever you sign in to Windows.'
    }
}

# ---- 11. NVIDIA key and first start ------------------------------------------------------
Step 11 'Your free NVIDIA key (Jarvis''s brain)'
if (-not (Get-KeyOk) -and -not $Unattended) {
    Say 'Jarvis thinks with NVIDIA''s free AI models, so it needs your own free NVIDIA key:' 'Yellow'
    Say '  1. In the web page that opens now, sign in to NVIDIA (or create a free account).'
    Say '  2. Click "Generate API Key" (or, on any model''s page, "Get API Key") and copy the key.'
    Say '     It starts with nvapi-'
    Say '  3. Paste it into the Notepad window that opens, press Ctrl+S to save, and close Notepad.'
    Say '  Keep the key private, like a password.'
    Start-Process 'https://build.nvidia.com/settings/api-keys'
    Start-Process notepad.exe -ArgumentList "`"$keyFile`""
    for ($i = 0; $i -lt 5 -and -not (Get-KeyOk); $i++) {
        $answer = Ask 'When the key is saved, press Enter here (or type S and Enter to skip for now)'
        if ($answer -match '^\s*s') { break }
        if (-not (Get-KeyOk)) { Say 'No key found in the file yet. Paste the key (it starts with nvapi-), press Ctrl+S, then press Enter here.' 'Yellow' }
    }
}

if ($NoStart) {
    Say 'Setup finished (Jarvis not started: -NoStart).' 'Green'
    exit 0
}
if (-not (Get-KeyOk)) {
    Write-Host ''
    Say 'Setup finished, but there is no NVIDIA key yet.' 'Yellow'
    Say "Paste your key into $keyFile, save it, then double-click the Jarvis icon on your Desktop."
    exit 0
}

Say 'Starting Jarvis for the first time (1-3 minutes)...'
if ($AutoStart -eq 'task') { Start-ScheduledTask -TaskName 'OpenJarvis' }
else { Start-Process -FilePath $conhost -ArgumentList @('--headless', 'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$Root\start-jarvis.ps1`"") }
$status = Join-Path $Root 'logs\status.txt'
for ($i = 0; $i -lt 600 -and -not (Test-JarvisUp); $i++) {
    Start-Sleep -Seconds 1
    if ($i -gt 20 -and (Test-Path $status) -and ((Get-Content $status -Raw) -match 'NOT STARTED|STOPPED:')) { break }
}
if (-not (Test-JarvisUp)) {
    $why = if (Test-Path $status) { (Get-Content $status -Raw).Trim() } else { 'no status' }
    Fail "Jarvis didn't start: $why"
}
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($edge) { Start-Process -FilePath $edge -ArgumentList "--app=$Url/" } else { Start-Process "$Url/" }

Write-Host ''
Say '  ============================================' 'Green'
Say '     Jarvis is installed and running!' 'Green'
Say '  ============================================' 'Green'
Say '  - Open it any time with the "Jarvis" icon on your Desktop.'
Say '  - Say "Hey Jarvis", or hold the RIGHT Ctrl key and talk.'
Say '  - The guide is in the JARVIS folder on your Desktop: "How to use Jarvis".'
Say '  - The first spoken reply can take a few minutes: the voice downloads once (about 1 GB).'
exit 0
