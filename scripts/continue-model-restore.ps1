$ErrorActionPreference = "Stop"

$currentPid = 24736
$current = Get-Process -Id $currentPid -ErrorAction SilentlyContinue
if ($current) {
    Wait-Process -Id $currentPid
}

& "C:\Users\egza_\Documents\projets\agents\scripts\restore-openweight-models.ps1"
