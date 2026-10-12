<#
Installs AchTrack on Windows from this folder.

    powershell -ExecutionPolicy Bypass -File install.ps1

It makes a private Python environment under %LOCALAPPDATA%\AchTrack, installs AchTrack into it and
adds a Start Menu shortcut. Nothing is installed system-wide and nothing needs administrator rights.
Run it again after `git pull` to update (or use Help > Check for updates in the app).
#>
param([switch]$Quiet)

$ErrorActionPreference = "Stop"
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$Data = Join-Path $env:LOCALAPPDATA "AchTrack"
$Venv = Join-Path $Data "venv"

function Say($text) { if (-not $Quiet) { Write-Host $text } else { Write-Output $text } }

# A Python new enough to run it: the py launcher if there is one, else whatever "python" is.
$python = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    try {
        $exe = $candidate[0]
        $rest = @($candidate | Select-Object -Skip 1)
        $version = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.9") { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    Say "AchTrack needs Python 3.9 or newer. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}

New-Item -ItemType Directory -Force -Path $Data | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Say "Creating AchTrack's Python environment..."
    $exe = $python[0]
    $rest = @($python | Select-Object -Skip 1)
    & $exe @rest -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Say "Couldn't create the environment."; exit 1 }
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Windowless = Join-Path $Venv "Scripts\pythonw.exe"

Say "Installing AchTrack (the first time downloads Qt, about a minute)..."
& $VenvPython -m pip install --quiet --disable-pip-version-check --upgrade pip
& $VenvPython -m pip install --quiet --disable-pip-version-check --upgrade $Source
if ($LASTEXITCODE -ne 0) { Say "The install failed - see the messages above."; exit 1 }

# Where this copy came from, so the app's own update button can find it again.
# (Written this way because Windows PowerShell's "-Encoding UTF8" adds a byte-order mark.)
[System.IO.File]::WriteAllText((Join-Path $Data "source"), $Source, (New-Object System.Text.UTF8Encoding $false))

$Icon = & $VenvPython -c "import achtrack, os; print(os.path.join(os.path.dirname(achtrack.__file__), 'assets', 'achtrack.ico'))"
$Programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut((Join-Path $Programs "AchTrack.lnk"))
$Shortcut.TargetPath = $Windowless
$Shortcut.Arguments = "-m achtrack"
$Shortcut.WorkingDirectory = $Data
$Shortcut.IconLocation = $Icon
$Shortcut.Description = "Achievement and playtime tracker"
$Shortcut.Save()

Say ""
Say "Installed. Open AchTrack from the Start Menu."
Say "From a terminal:  & '$VenvPython' -m achtrack scan"
exit 0
