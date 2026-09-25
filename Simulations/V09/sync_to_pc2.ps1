param (
    [string]$Destination = ""
)

if ($Destination -eq "") {
    $Destination = Read-Host "Enter the UNC path to the Option B folder on PC 2 (e.g., \\192.168.100.2\Share\Option B)"
}

if (-not (Test-Path $Destination)) {
    Write-Error "Destination path not accessible: $Destination"
    exit 1
}

Write-Host "[*] Syncing code to $Destination..."

# Sync all files, excluding .env, results, and caches. /MIR mirrors the directory (deletes missing files).
robocopy . $Destination /MIR /XD runs runs_v1 __pycache__ .git /XF .env *.zip *.pdf *.pyc

# robocopy exit codes: 0-7 are success (1 = files copied, 2 = extra files deleted, etc.)
if ($LASTEXITCODE -ge 8) {
    Write-Error "Robocopy failed with exit code $LASTEXITCODE"
    exit 1
}

Write-Host "`n[+] Sync complete!"
Write-Host "⚠️  IMPORTANT: Because Python files were updated, you MUST rebuild the agents on PC 2!"
Write-Host "    Go to PC 2 and run:"
Write-Host "    docker compose -f docker-compose.pc2.yml up -d --build"
