<#
Removes AchTrack from Windows. Your library and settings are kept unless you pass -Everything.

    powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-Everything]
#>
param([switch]$Everything)

$Data = Join-Path $env:LOCALAPPDATA "AchTrack"
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AchTrack.lnk")
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "AchTrack" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Data "venv")
if ($Everything) {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Data
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $env:APPDATA "AchTrack")
    Write-Host "Removed AchTrack with its library and settings."
} else {
    Write-Host "Removed AchTrack. Your library is still in $Data (run again with -Everything to delete it)."
}
