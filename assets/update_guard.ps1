param([Parameter(Mandatory=$true)][string]$Executable, [int]$TimeoutSeconds = 240)
$ErrorActionPreference = 'Stop'
$pending = $Executable + '.update-pending.json'
$previous = $Executable + '.old'
$ready = $Executable + '.update-ready'
$guardLog = $Executable + '.update.log'
$process = $null
$stage = 'metadata'
function Record([string]$eventName) {
    Add-Content -LiteralPath $guardLog -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $eventName)
}
try {
    $metadata = Get-Content -LiteralPath $pending -Raw | ConvertFrom-Json
    $stage = 'validate_files'
    if ((Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash -ne $metadata.new_hash -or
        (Get-FileHash -LiteralPath $previous -Algorithm SHA256).Hash -ne $metadata.old_hash) {
        throw 'Update files changed; manual recovery required'
    }
    Remove-Item -LiteralPath $ready -Force -ErrorAction SilentlyContinue
    $stage = 'launch'
    $env:MIREA_UPDATE_NONCE = $metadata.nonce
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'
    Remove-Item Env:MIREA_ASSISTANT_CHILD -ErrorAction SilentlyContinue
    try {
        $process = Start-Process -FilePath $Executable -WorkingDirectory (Split-Path -LiteralPath $Executable) -WindowStyle Hidden -PassThru
    } catch { Record 'update_launch_failed' }
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $stage = 'confirmation'
    while ($null -ne $process -and [DateTime]::UtcNow -lt $deadline) {
        if ((Test-Path -LiteralPath $ready) -and
            (Get-Content -LiteralPath $ready -Raw) -eq $metadata.nonce -and -not $process.HasExited) {
            Remove-Item -LiteralPath $pending -Force
            Remove-Item -LiteralPath $ready -Force
            Record 'update_committed'
            exit 0
        }
        if ($process.HasExited) { break }
        Start-Sleep -Seconds 1
        $process.Refresh()
    }
    # A deliberate exit is not a crash. Keep the backup until the next launch.
    if ($null -ne $process -and $process.HasExited -and $process.ExitCode -eq 0) {
        Record 'update_closed_before_confirmation'
        exit 0
    }
    Record 'update_rollback_started'
    $stage = 'rollback_stop'
    if ($null -ne $process -and -not $process.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
        if (-not $process.WaitForExit(15000)) { throw 'Update process did not stop' }
    }
    $stage = 'rollback_validate'
    if ((Get-FileHash -LiteralPath $previous -Algorithm SHA256).Hash -ne $metadata.old_hash) {
        throw 'Backup changed; manual recovery required'
    }
    # Keep the failed executable for diagnosis; retry transient antivirus locks.
    $moved = $false
    $stage = 'rollback_replace'
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        try {
            if (Test-Path -LiteralPath $Executable) {
                Move-Item -LiteralPath $Executable -Destination ($Executable + '.failed-' + $metadata.nonce) -Force
            }
            Move-Item -LiteralPath $previous -Destination $Executable
            $moved = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $moved) { throw 'Cannot restore backup' }
    Remove-Item -LiteralPath $pending -Force
    Remove-Item -LiteralPath $ready -Force -ErrorAction SilentlyContinue
    Remove-Item Env:MIREA_UPDATE_NONCE -ErrorAction SilentlyContinue
    $stage = 'rollback_launch'
    Start-Process -FilePath $Executable -WorkingDirectory (Split-Path -LiteralPath $Executable) -WindowStyle Hidden
    Record 'update_rolled_back'
} catch {
    # Fixed stage/type identifiers help diagnose a failed rollback without
    # printing paths, exception messages, transaction contents or its nonce.
    $errorType = $_.Exception.GetType().Name
    $errorId = ($_.FullyQualifiedErrorId.Split(',')[0] -replace '[^a-zA-Z0-9_.-]', '')
    Record ('update_guard_failed_manual_recovery_required stage=' + $stage + ' type=' + $errorType + ' id=' + $errorId)
    exit 1
} finally {
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
