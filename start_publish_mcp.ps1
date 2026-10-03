$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$runtimeDir = Join-Path $projectDir 'runtime'
$pythonExe = Join-Path $projectDir '.venv\Scripts\python.exe'

# A manually started service can already own the endpoint at login.
$listener = Get-NetTCPConnection -LocalPort 5410 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    foreach ($connection in $listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
        if ($owner.CommandLine -notmatch 'publish_mcp\.py') {
            throw 'Port 5410 is occupied by another application.'
        }
    }
    exit 0
}

New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$service = Start-Process -FilePath $pythonExe -ArgumentList 'publish_mcp.py' `
    -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $runtimeDir "mcp-autostart-$stamp.out.log") `
    -RedirectStandardError (Join-Path $runtimeDir "mcp-autostart-$stamp.err.log")
$service.Id | Set-Content -LiteralPath (Join-Path $runtimeDir 'mcp.pid')
# Keep the scheduled task alive so failure restarts follow the real process.
$service.WaitForExit()
exit $service.ExitCode
