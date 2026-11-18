# Registers a Windows Task Scheduler job that runs the daily pipeline at 06:00 local time.
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
param([string]$Time = "06:00")

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $python -Argument "-m mqcs daily" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "MediaQuotesDaily" -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Scheduled 'MediaQuotesDaily' at $Time (python -m mqcs daily in $repo)."
