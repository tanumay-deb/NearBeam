# NearBeam 1-Line Windows Web Installer
# Usage: irm https://raw.githubusercontent.com/tanumay-deb/NearBeam/main/install.ps1 | iex

$ErrorActionPreference = "Stop"
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "       NearBeam 1-Line Installer        " -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$Repo = "tanumay-deb/NearBeam"
$ApiUrl = "https://api.github.com/repos/$Repo/releases/latest"

try {
    Write-Host "[1/3] Querying latest release from GitHub..." -ForegroundColor Yellow
    $Release = Invoke-RestMethod -Uri $ApiUrl -Headers @{"User-Agent"="NearBeam-Installer"}
    $Tag = $Release.tag_name
    Write-Host "      Found release: $Tag" -ForegroundColor Green

    # Find installer asset (.exe)
    $SetupAsset = $Release.assets | Where-Object { $_.name -like "*Setup*.exe" } | Select-Object -First 1
    if (-not $SetupAsset) {
        $SetupAsset = $Release.assets | Where-Object { $_.name -like "*.exe" } | Select-Object -First 1
    }

    if ($SetupAsset) {
        $DownloadUrl = $SetupAsset.browser_download_url
        $TempFile = Join-Path $env:TEMP $SetupAsset.name

        Write-Host "[2/3] Downloading $($SetupAsset.name)..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $DownloadUrl -OutFile $TempFile -UseBasicParsing

        Write-Host "[3/3] Launching NearBeam Setup..." -ForegroundColor Green
        Start-Process -FilePath $TempFile
        Write-Host "Setup launched! Follow the on-screen prompts to complete installation." -ForegroundColor Green
    } else {
        # Fallback to pip install if python is available
        Write-Host "Release binary not yet published, falling back to pip..." -ForegroundColor Yellow
        if (Get-Command "pip" -ErrorAction SilentlyContinue) {
            Write-Host "Installing via pip from GitHub..." -ForegroundColor Yellow
            pip install "git+https://github.com/$Repo.git"
            Write-Host "NearBeam installed! Type 'nearbeam' from any terminal." -ForegroundColor Green
            Start-Process "nearbeam"
        } else {
            Write-Error "Could not find release binary or Python pip."
        }
    }
} catch {
    Write-Error "Installation failed: $($_.Exception.Message)"
}
