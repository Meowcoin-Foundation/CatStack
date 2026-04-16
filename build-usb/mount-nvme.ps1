# Mount NVMe in WSL (requires admin)
Write-Host "Mounting NVMe (PHYSICALDRIVE0) in WSL..."
wsl --mount '\\.\PHYSICALDRIVE0' --bare
if ($LASTEXITCODE -eq 0) {
    Write-Host "SUCCESS - NVMe is now available in WSL" -ForegroundColor Green
    Write-Host "You can close this window."
} else {
    Write-Host "FAILED - exit code $LASTEXITCODE" -ForegroundColor Red
}
Read-Host "Press Enter to close"
