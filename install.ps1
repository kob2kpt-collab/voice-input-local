$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "Voice Input Local installer"
Write-Host "Folder: $PSScriptRoot"

$Python = "py"
$PyArgs = $null
try {
  & $Python -3.11 --version | Out-Host
  $PyArgs = @("-3.11")
} catch {
  try {
    & $Python -3.10 --version | Out-Host
    $PyArgs = @("-3.10")
  } catch {
    Write-Host "Python 3.10 or 3.11 is required. Install it from https://www.python.org/downloads/windows/ and enable Add python.exe to PATH."
    exit 1
  }
}

if (-not (Test-Path -LiteralPath "requirements.txt")) {
  Write-Host "ERROR: requirements.txt was not found. Extract the ZIP archive completely and run install.bat from the extracted folder."
  exit 1
}

& $Python @PyArgs -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Voice Input Local.lnk"
$TargetPath = Join-Path (Get-Location) "run.bat"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.WorkingDirectory = (Get-Location).Path
$Shortcut.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,168"
$Shortcut.Save()

Write-Host "Installation completed. Run run.bat or use the desktop shortcut."
