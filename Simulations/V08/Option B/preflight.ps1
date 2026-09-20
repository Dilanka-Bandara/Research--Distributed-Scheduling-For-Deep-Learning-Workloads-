Write-Host "[*] Starting Redis container on PC 1 for preflight tests..."
docker compose -f docker-compose.pc1.yml up -d redis

Write-Host "[*] Running preflight diagnostic..."
# Run the python script using the tools profile to ensure it has all dependencies (redis, etc.)
docker compose -f docker-compose.pc1.yml --profile tools run --rm orchestrator python preflight.py

Write-Host "[*] Shutting down Redis..."
docker compose -f docker-compose.pc1.yml down
