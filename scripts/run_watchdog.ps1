# Starts the PolyBot watchdog loop (detached, hidden) if not already running.
# Called from the Startup folder at logon, or manually.
$repo = "C:\workspace\space\futurePrediction"
Set-Location $repo

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'watchdog\.py' -and $_.CommandLine -match '--loop' }
if ($existing) {
    Write-Output "watchdog loop already running (pid $($existing.ProcessId))"
    exit 0
}

Start-Process -WindowStyle Hidden -WorkingDirectory $repo python `
    -ArgumentList "scripts\watchdog.py", "--loop"
Write-Output "watchdog loop started"
