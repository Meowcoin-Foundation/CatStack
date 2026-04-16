# MeowOS install to ADATA SU650
# Phase 1: Shutdown WSL to clear all stale state
# Phase 2: Build image in WSL (loop devices, no disk passthrough)
# Phase 3: dd image to SSD from PowerShell
$ErrorActionPreference = "Stop"

try {
    $disk = Get-Disk -Number 4
    if ($disk.FriendlyName -notlike "*ADATA*SU650*") { throw "Disk 4 is not ADATA SU650!" }
    Write-Host "Target: $($disk.FriendlyName)" -ForegroundColor Green

    # Phase 1: Kill WSL completely to clear stale loops/mounts
    Write-Host "`n=== Resetting WSL ===" -ForegroundColor Cyan
    wsl --shutdown 2>&1
    Start-Sleep 5

    # Phase 2: Build image
    Write-Host "`n=== Building MeowOS image (~15 min) ===" -ForegroundColor Cyan
    Remove-Item "C:\Source\meowos.img" -ErrorAction SilentlyContinue

    $proc = Start-Process -FilePath "wsl" -ArgumentList "-d Ubuntu-22.04 -- sudo bash /mnt/c/Source/mfarm/build-usb/wsl-build-image.sh" -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Host "WSL build failed with exit code $($proc.ExitCode)" -ForegroundColor Red
    }

    $imgPath = "C:\Source\meowos.img"
    if (-not (Test-Path $imgPath)) { throw "Image not found at $imgPath - build failed" }
    $imgSize = (Get-Item $imgPath).Length
    Write-Host "Image ready: $([math]::Round($imgSize/1GB, 2)) GB" -ForegroundColor Green

    # Phase 3: Write to SSD
    Write-Host "`n=== Writing to ADATA SSD ===" -ForegroundColor Cyan
    Set-Disk -Number 4 -IsOffline $false
    try { Clear-Disk -Number 4 -RemoveData -RemoveOEM -Confirm:$false } catch {}
    Set-Disk -Number 4 -IsOffline $true
    Start-Sleep 2

    $physicalDrive = "\\.\PhysicalDrive4"
    $bufferSize = 4MB
    $bytesWritten = 0

    $source = [System.IO.File]::OpenRead($imgPath)
    $dest = [System.IO.File]::Open($physicalDrive, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    $buffer = New-Object byte[] $bufferSize

    while (($read = $source.Read($buffer, 0, $bufferSize)) -gt 0) {
        $dest.Write($buffer, 0, $read)
        $bytesWritten += $read
        $pct = [math]::Round(($bytesWritten / $imgSize) * 100, 1)
        Write-Host "`r  $pct% ($([math]::Round($bytesWritten/1MB)) / $([math]::Round($imgSize/1MB)) MB)" -NoNewline
    }
    $dest.Flush()
    $source.Close()
    $dest.Close()

    Write-Host ""
    Write-Host "$([math]::Round($bytesWritten/1MB)) MB written." -ForegroundColor Green

    Remove-Item $imgPath -ErrorAction SilentlyContinue

    # Phase 4: Fix GPT backup header
    # The 8GB image has GPT backup at 8GB mark, but disk is 120GB.
    # UEFI firmware sees corrupt GPT and refuses to boot without this fix.
    Write-Host "`n=== Fixing GPT for 120GB disk ===" -ForegroundColor Cyan
    Set-Disk -Number 4 -IsOffline $true
    Start-Sleep 2
    wsl --mount '\\.\PhysicalDrive4' --bare 2>&1
    Start-Sleep 3
    wsl -d Ubuntu-22.04 -- sudo bash /mnt/c/Source/mfarm/build-usb/fix-gpt.sh 2>&1 | ForEach-Object { Write-Host $_ }
    wsl --unmount '\\.\PhysicalDrive4' 2>&1
    Set-Disk -Number 4 -IsOffline $false

    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  MeowOS installed on ADATA SU650!" -ForegroundColor Green
    Write-Host "  GPT fixed for full disk size." -ForegroundColor Green
    Write-Host "  Plug into rig and boot." -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
    "SUCCESS" | Out-File "C:\Source\flash-usb-result.txt"
} catch {
    Write-Host "`nERROR: $_" -ForegroundColor Red
    "FAILED: $_" | Out-File "C:\Source\flash-usb-result.txt"
}
Read-Host "Press Enter to close"
