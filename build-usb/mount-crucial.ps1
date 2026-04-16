# Mount Crucial 1TB SSD (PHYSICALDRIVE0) in WSL
Write-Host "Mounting Crucial SSD (PHYSICALDRIVE0) in WSL..."
wsl --mount '\\.\PHYSICALDRIVE0' --bare
if ($LASTEXITCODE -eq 0) {
    Write-Host "SUCCESS - SSD is now available in WSL" -ForegroundColor Green
} else {
    Write-Host "FAILED - exit code $LASTEXITCODE" -ForegroundColor Red
}
Read-Host "Press Enter to close"
