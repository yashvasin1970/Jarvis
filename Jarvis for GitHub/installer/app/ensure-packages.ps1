# Puts back Jarvis packages that a package update removed. Run by
# start-jarvis.ps1 before every start; takes about a second when nothing is
# missing. Log: logs\packages.log
#
# Why: `uv sync` installs only what the command line asks for, and removes the
# rest. The official installer syncs "--extra desktop --group desktop-native",
# which drops the PDF and voice (Kokoro) extras, the spaCy English model
# (not on PyPI) and NVIDIA cuBLAS 12 (GPU listening), and swaps the GPU build
# of PyTorch for the CPU one.
# Keep this file ASCII-only (Windows PowerShell 5.1 reads it as ANSI).

$ErrorActionPreference = 'Continue'
$Root  = $PSScriptRoot    # the Jarvis folder (this script lives in it)

# Values from JARVIS-SETTINGS.txt next to this script (lines like GPU=cuda; # = comment).
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
# GPU=cpu (set by the setup kit on PCs without a suitable NVIDIA card): don't ask for
# the GPU builds of PyTorch / cuBLAS. Unset or GPU=cuda: they're required.
$UseGpu = (Get-JarvisSetting 'GPU' 'cuda') -ne 'cpu'
# The Rust part (openjarvis_rust) is normally compiled from source by `uv sync`. PCs set
# up by the kit have no Rust compiler, so there it's installed from the ready-built wheel
# in cache\wheels instead, and the sync leaves it alone.
$HaveRust  = Test-Path (Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe')
$RustWheel = Get-ChildItem (Join-Path $Root 'cache\wheels') -Filter 'openjarvis_rust-*.whl' -ErrorAction SilentlyContinue |
             Select-Object -First 1
$Src   = Join-Path $Root 'src'
$Py    = Join-Path $Src '.venv\Scripts\python.exe'
$Uv    = Join-Path $Root 'tools\uv.exe'
$Log   = Join-Path $Root 'logs\packages.log'

# Everything Jarvis needs, as `uv sync` options (all versions come from uv.lock).
$SyncArgs = @('sync', '--frozen', '--inexact',
              '--extra', 'desktop', '--extra', 'pdf', '--extra', 'voice')
if ($HaveRust -or -not $RustWheel) { $SyncArgs += @('--group', 'desktop-native') }
# spaCy's English model (Kokoro pronunciation); kept as a local file so a
# repair also works offline.
$SpacyWheel = Join-Path $Root 'cache\wheels\en_core_web_sm-3.8.0-py3-none-any.whl'
# GPU build of PyTorch for the Kokoro voice (RTX 3050, NVIDIA driver with CUDA 13).
$TorchFlavor = 'cu130'
# NVIDIA cuBLAS 12 for GPU listening (faster-whisper / CTranslate2 4.x), as a
# local file so a repair works offline. start-jarvis.ps1 puts its bin on PATH.
$CublasWheel = Join-Path $Root 'cache\wheels\nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl'

$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root 'python'
$env:UV_CACHE_DIR          = Join-Path $Root 'cache\uv'
$env:UV_PYTHON_PREFERENCE  = 'only-managed'
$env:UV_HTTP_TIMEOUT       = '600'
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"   # only used if the Rust part must be rebuilt
$env:JARVIS_GPU_CHECKS = $(if ($UseGpu) { '1' } else { '0' })   # read by the probe below
# A copy without git history (the setup kit) can't work out its version number from git.
if (-not (Test-Path (Join-Path $Src '.git'))) { $env:SETUPTOOLS_SCM_PRETEND_VERSION = '1.0.5' }

function Write-Log([string] $text) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $text" | Out-File -FilePath $Log -Append -Encoding utf8
}

# Runs uv with the given arguments, logs its output, returns the exit code.
function Invoke-Uv([string] $what, [string[]] $uvArgs) {
    $out = & $Uv @uvArgs 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-Log "$what (exit $code)`r`n$($out.Trim())"
    return $code
}

# Prints one line per missing piece, or OK. Never imports torch (slow).
$probe = @'
import importlib.util as u, importlib.metadata as m
need = ["openjarvis_rust", "fastapi", "faster_whisper", "kokoro", "misaki",
        "spacy", "espeakng_loader", "pdfplumber", "en_core_web_sm"]
missing = [n for n in need if u.find_spec(n) is None]
try:
    tv = m.version("torch")
except m.PackageNotFoundError:
    tv = ""
import os, sysconfig
gpu = os.environ.get("JARVIS_GPU_CHECKS", "1") == "1"
if gpu and tv and "+cu" not in tv:
    missing.append("torch-gpu:" + tv.split("+")[0])
if gpu and not os.path.exists(os.path.join(sysconfig.get_paths()["purelib"], "nvidia", "cublas", "bin", "cublas64_12.dll")):
    missing.append("cublas12")
print("\n".join(missing) or "OK")
'@

function Get-Missing {
    # Fed through stdin: PowerShell 5.1 mangles double quotes in native arguments.
    $out = $probe | & $Py -I - 2>&1
    if ($LASTEXITCODE -ne 0) { return @("python-broken: $out") }
    return @($out | Where-Object { $_ -and $_ -ne 'OK' })
}

# Windows 11 Smart App Control can stop trusting the python.exe launchers that uv writes into
# each environment: they contain the folder path, so they differ on every PC and Windows has
# never seen them (on 2026-11-08 it suddenly blocked two that had worked). Python's own
# standard launcher is the same file everywhere and stays trusted, so make sure every
# environment uses it (it reads the same pyvenv.cfg, so nothing else changes).
foreach ($venv in 'src\.venv', 'image-tools\.venv', 'google-tools\.venv', 'voice-popup\.venv') {
    $cfg = Join-Path $Root "$venv\pyvenv.cfg"
    if (-not (Test-Path $cfg)) { continue }
    $pyHome = ((Get-Content $cfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1) -replace '^\s*home\s*=\s*', '').Trim()
    foreach ($exe in 'python.exe', 'pythonw.exe') {
        $std = Join-Path $pyHome "Lib\venv\scripts\nt\$exe"
        $mine = Join-Path $Root "$venv\Scripts\$exe"
        if ((Test-Path $std) -and (Test-Path $mine) -and ((Get-FileHash $std).Hash -ne (Get-FileHash $mine).Hash)) {
            try { Copy-Item $std $mine -Force; Write-Log "Replaced $venv\Scripts\$exe with Python's standard launcher." }
            catch { Write-Log "Could not replace $venv\Scripts\$exe : $_" }
        }
    }
}

$missing = Get-Missing
if ($missing.Count -eq 0) { exit 0 }
Write-Log "Missing after an update: $($missing -join ', ')"
if ($missing[0] -like 'python-broken*') { Write-Log 'Python environment is broken; not attempting repair.'; exit 1 }

# 1) Packages from the lock file (online first, then from the local cache).
$fromLock = $missing | Where-Object { $_ -notlike 'torch-gpu:*' -and $_ -ne 'en_core_web_sm' -and $_ -ne 'cublas12' -and
                                      -not ($_ -eq 'openjarvis_rust' -and -not $HaveRust -and $RustWheel) }
if ($fromLock) {
    Push-Location $Src
    $code = Invoke-Uv 'uv sync' $SyncArgs
    if ($code -ne 0) { [void](Invoke-Uv 'uv sync (offline)' ($SyncArgs + '--offline')) }
    Pop-Location
    $missing = Get-Missing   # sync may have swapped PyTorch back to the CPU build
}

# 2) Ready-built Rust part (PCs without a Rust compiler), spaCy English model, cuBLAS.
if ($missing -contains 'openjarvis_rust' -and -not $HaveRust -and $RustWheel) {
    [void](Invoke-Uv 'Rust part (ready-built)' @('pip', 'install', '--offline', '--no-deps', '--python', $Py, $RustWheel.FullName))
}
if ($missing -contains 'en_core_web_sm' -and (Test-Path $SpacyWheel)) {
    [void](Invoke-Uv 'spaCy English model' @('pip', 'install', '--offline', '--python', $Py, $SpacyWheel))
}
if ($missing -contains 'cublas12' -and (Test-Path $CublasWheel)) {
    [void](Invoke-Uv 'NVIDIA cuBLAS 12' @('pip', 'install', '--offline', '--no-deps', '--python', $Py, $CublasWheel))
}

# 3) GPU build of PyTorch, same version as the lock file chose. If that version
#    has no GPU build yet, keep the CPU build: the voice still works, just slower.
$torch = $missing | Where-Object { $_ -like 'torch-gpu:*' } | Select-Object -First 1
if ($torch -and $UseGpu -and (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    $ver = $torch.Split(':')[1]
    [void](Invoke-Uv "GPU PyTorch $ver+$TorchFlavor" @('pip', 'install', '--python', $Py, '--no-deps',
        '--reinstall-package', 'torch', '--index-url', "https://download.pytorch.org/whl/$TorchFlavor",
        "torch==$ver+$TorchFlavor"))
}

$left = Get-Missing
if ($left.Count -eq 0) { Write-Log 'Repair complete.'; exit 0 }
Write-Log "Still missing after repair: $($left -join ', ')"
exit 1
