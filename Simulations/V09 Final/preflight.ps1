# preflight.ps1 — V09. Run on PC 1 with PC 2's agents already up.
#
# FIX: the V08 script ran `run --rm orchestrator python preflight.py`. The
# orchestrator service has a fixed entrypoint (python orchestrator.py), so
# those words were passed to orchestrator.py as arguments instead of running
# preflight.py. --entrypoint overrides it.

Write-Host "[*] Starting Redis on PC 1..."
docker compose -f docker-compose.pc1.yml --profile tools up -d redis

Write-Host "[*] Giving PC 2's agents a few seconds to reconnect..."
Start-Sleep -Seconds 6

Write-Host "[*] Running preflight..."
docker compose -f docker-compose.pc1.yml --profile tools run --rm --entrypoint python orchestrator preflight.py
$code = $LASTEXITCODE

Write-Host "[*] Stopping Redis..."
docker compose -f docker-compose.pc1.yml --profile tools down

if ($code -ne 0) { Write-Host "[-] PREFLIGHT FAILED - fix the issue above before running."; exit 1 }
Write-Host "[+] Preflight passed."
