# Starts the OpenJarvis web chat server (http://127.0.0.1:PORT, normally 8000) in the background.
# Run at logon by the Windows scheduled task "OpenJarvis"; also used by the
# "Jarvis" and "Restart Jarvis" shortcuts in Desktop\JARVIS.
# Keep this file ASCII-only (Windows PowerShell 5.1 reads it as ANSI).

$ErrorActionPreference = 'Stop'
$Root      = $PSScriptRoot    # the Jarvis folder (this script lives in it)
$KeyFile   = Join-Path $Root 'NVIDIA-API-KEY.txt'
$LogDir    = Join-Path $Root 'logs'
$StatusFile = Join-Path $LogDir 'status.txt'
# "jarvis serve" is run as `python -m openjarvis.cli serve`: the same program, but without the
# jarvis.exe launcher that uv writes for each PC (Smart App Control blocks such new .exe files).
$Jarvis    = Join-Path $Root 'src\.venv\Scripts\python.exe'
$Desktop   = [Environment]::GetFolderPath('Desktop')
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
$Port = Get-JarvisSetting 'PORT' '8000'
$Url  = "http://127.0.0.1:$Port"
# Only set for a test install made with the setup kit: its own Desktop folder and its
# own settings folder instead of <user>\.openjarvis.
$d = Get-JarvisSetting 'DESKTOP' ''; if ($d) { $Desktop = $d }
$h = Get-JarvisSetting 'JARVIS_HOME' ''; if ($h) { $env:OPENJARVIS_HOME = $h }

# Models shown in the web chat's model picker. Tested 2026-11-04 with this
# account's free key: all three answer quickly and use tools correctly.
# (kimi-k2.6 and nemotron-3-super returned "Not found for account".)
$Models = @(
    'nvidia/nemotron-3.5-super-vl-120b-a12b',
    'nvidia/nemotron-3.5-lightning-30b-a3b',
    'nvidia/nemotron-3-ultra-550b-a55b'
)

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Set-Status([string] $text) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$stamp  $text" | Out-File -FilePath $StatusFile -Encoding utf8
}

function Test-JarvisUp {
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (Test-JarvisUp) { Set-Status 'Jarvis is already running.'; exit 0 }

# --- NVIDIA key (pasted by the user into NVIDIA-API-KEY.txt) -----------------
$key = ''
if (Test-Path $KeyFile) { $keyRaw = Get-Content -LiteralPath $KeyFile -Raw; if ($keyRaw) { $key = "$keyRaw" } }
$key = $key.Trim().Trim('"', "'").Trim()
$key = $key -replace '^(NIM_API_KEY|NVIDIA_API_KEY)\s*=\s*', ''
$key = $key.Trim().Trim('"', "'").Trim()
if (-not $key.StartsWith('nvapi-')) {
    Set-Status "NOT STARTED: no NVIDIA key found. Paste your key (it starts with nvapi-) into $KeyFile, save it, then use the 'Restart Jarvis' shortcut."
    exit 2
}

# --- Telegram bot token (optional; pasted by the user into TELEGRAM-BOT-TOKEN.txt).
# Telegram itself is switched on in config.toml [channel] only for the user's own
# chat id, so nobody else can talk to this Jarvis through the bot.
# (An empty file must not stop Jarvis: Get-Content -Raw returns $null for it.)
$TgFile = Join-Path $Root 'TELEGRAM-BOT-TOKEN.txt'
try {
    $tgRaw = $null
    if (Test-Path $TgFile) { $tgRaw = Get-Content -LiteralPath $TgFile -Raw }
    if ($tgRaw) {
        $tg = "$tgRaw".Trim().Trim('"', "'").Trim()
        if ($tg -match '^\d{5,}:[A-Za-z0-9_-]{30,}$') { $env:TELEGRAM_BOT_TOKEN = $tg }
    }
} catch { }

# --- Put back packages a Jarvis update removed (voice, PDF, GPU PyTorch) -------
Set-Status 'Checking Jarvis packages (a repair after an update can take a few minutes)...'
& (Join-Path $Root 'ensure-packages.ps1')

# --- Environment for the server ------------------------------------------------
$env:NIM_API_KEY = $key
$env:NIM_MODELS = ($Models -join ',')
$env:OPENJARVIS_TASK_RESULTS_DIR = Join-Path $Desktop 'JARVIS\Scheduled task results'
$env:PYTHONUTF8 = '1'             # non-English file names work with the Desktop tools
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'       # write the log as it happens
$env:NO_COLOR = '1'
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$env:OPENJARVIS_INCLUDE_DATETIME = '1'   # tell the AI today's date and time
# GPU listening: faster-whisper (CTranslate2) needs NVIDIA cuBLAS 12, installed
# as the nvidia-cublas-cu12 package (ensure-packages.ps1 keeps it there).
$env:Path = (Join-Path $Root 'src\.venv\Lib\site-packages\nvidia\cublas\bin') + ';' + $env:Path

# Windows time zone -> IANA name, so "every day at 8 AM" means 8 AM here.
$tzMap = @{
    'India Standard Time' = 'Asia/Kolkata'; 'Sri Lanka Standard Time' = 'Asia/Colombo'
    'Nepal Standard Time' = 'Asia/Kathmandu'; 'Bangladesh Standard Time' = 'Asia/Dhaka'
    'Arabian Standard Time' = 'Asia/Dubai'; 'Singapore Standard Time' = 'Asia/Singapore'
    'China Standard Time' = 'Asia/Shanghai'; 'Tokyo Standard Time' = 'Asia/Tokyo'
    'GMT Standard Time' = 'Europe/London'; 'W. Europe Standard Time' = 'Europe/Berlin'
    'Central European Standard Time' = 'Europe/Warsaw'; 'Romance Standard Time' = 'Europe/Paris'
    'Eastern Standard Time' = 'America/New_York'; 'Central Standard Time' = 'America/Chicago'
    'Mountain Standard Time' = 'America/Denver'; 'Pacific Standard Time' = 'America/Los_Angeles'
    'AUS Eastern Standard Time' = 'Australia/Sydney'; 'UTC' = 'UTC'
}
$winTz = (Get-TimeZone).Id
if ($tzMap.ContainsKey($winTz)) { $env:OPENJARVIS_TIMEZONE = $tzMap[$winTz] }

# --- Local offline model (Ollama, in tools\ollama; models in .\models) ---------
$Ollama = Join-Path $Root 'tools\ollama\ollama.exe'
$env:OLLAMA_HOST = 'http://127.0.0.1:11434'   # only this PC can reach it
$env:OLLAMA_MODELS = Join-Path $Root 'models'
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_KV_CACHE_TYPE = 'q8_0'             # smaller working memory on the 4 GB GPU
$env:OLLAMA_KEEP_ALIVE = '30m'                 # free the GPU after 30 idle minutes
$env:OLLAMA_CONTEXT_LENGTH = '32768'
$env:JARVIS_NUM_CTX = '32768'                  # Jarvis prompts with tools reach ~24k tokens

function Test-OllamaUp {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/version' -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Start-Ollama {
    if (-not (Test-Path $Ollama) -or (Test-OllamaUp)) { return }
    Start-Process -FilePath $Ollama -ArgumentList 'serve' -WorkingDirectory $Root -NoNewWindow `
        -RedirectStandardOutput (Join-Path $LogDir 'ollama.log') `
        -RedirectStandardError (Join-Path $LogDir 'ollama.err.log') | Out-Null
    # Jarvis only lists local models if Ollama answers when Jarvis starts.
    for ($i = 0; $i -lt 30 -and -not (Test-OllamaUp); $i++) { Start-Sleep -Seconds 1 }
}

function Start-VoicePopup {
    # "Hey Jarvis" pop-up (own Python environment in voice-popup\; it exits at
    # once if it is already running). Started early so that while the server is
    # still loading it can at least say so.
    $popupPy = Join-Path $Root 'voice-popup\.venv\Scripts\pythonw.exe'
    if (Test-Path $popupPy) {
        Start-Process -FilePath $popupPy -WorkingDirectory (Join-Path $Root 'voice-popup') `
            -ArgumentList @('-I', "`"$(Join-Path $Root 'voice-popup\jarvis_voice.py')`"")
    }
}
Start-VoicePopup

# --- Run the server; restart it if it crashes (max 5 quick crashes) -----------
$crashes = @()
while ($true) {
    Start-Ollama
    $log = Join-Path $LogDir 'jarvis-server.log'
    if (Test-Path $log) { Move-Item -Force $log (Join-Path $LogDir 'jarvis-server.previous.log') }
    Set-Status 'Starting Jarvis...'
    $started = Get-Date
    $p = Start-Process -FilePath $Jarvis -ArgumentList @('-P', '-m', 'openjarvis.cli', 'serve') -WorkingDirectory $Root `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $LogDir 'jarvis-server.err.log') `
        -NoNewWindow -PassThru
    $null = $p.Handle   # keeps the handle so ExitCode is available later
    $p.Id | Out-File -FilePath (Join-Path $LogDir 'jarvis-server.pid') -Encoding ascii
    $up = $false
    for ($i = 0; $i -lt 300 -and -not $p.HasExited; $i++) {
        if (Test-JarvisUp) { $up = $true; Set-Status "Jarvis is running at $Url"; break }
        Start-Sleep -Seconds 1
    }
    # Warm up the voice: load Kokoro (speaking) and Whisper (listening) now, so
    # the first spoken reply or dictation in the web chat is quick (~50 s cold).
    if ($up) {
        try {
            $warm = Join-Path $LogDir 'voice-warmup.wav'
            Invoke-WebRequest -Uri "$Url/v1/speech/synthesize" -Method Post `
                -ContentType 'application/json' -Body '{"text": "Jarvis is ready."}' `
                -OutFile $warm -UseBasicParsing -TimeoutSec 300
            & "$env:SystemRoot\System32\curl.exe" -s -m 300 -F "file=@$warm;type=audio/wav" `
                "$Url/v1/speech/transcribe" | Out-Null
        } catch { }
        Start-VoicePopup   # in case it had stopped
    }
    $p.WaitForExit()
    $code = $p.ExitCode
    if (((Get-Date) - $started).TotalMinutes -gt 10) { $crashes = @() }
    $crashes += $started
    if ($crashes.Count -ge 5) {
        Set-Status "STOPPED: Jarvis crashed 5 times in a row (last exit code $code). See $LogDir\jarvis-server.err.log"
        exit 1
    }
    Set-Status "Jarvis stopped (exit code $code); restarting in 10 seconds..."
    Start-Sleep -Seconds 10
}
