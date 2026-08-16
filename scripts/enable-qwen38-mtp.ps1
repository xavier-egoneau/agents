$ErrorActionPreference = "Stop"

$providersPath = "C:\Users\egza_\Documents\content-agents\providers.json"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupPath = "$providersPath.before-qwen38-mtp-$stamp.bak"

Copy-Item -LiteralPath $providersPath -Destination $backupPath

$config = Get-Content -LiteralPath $providersPath -Raw | ConvertFrom-Json
$provider = $config.providers | Where-Object { $_.id -eq "llama-local" }
if ($null -eq $provider) {
    throw "Provider llama-local introuvable."
}

$provider.model = "qwen3.8-27b-ud-q4kxl"
$argsList = [System.Collections.Generic.List[string]]::new()
foreach ($arg in $provider.llama_args) {
    $argsList.Add([string]$arg)
}

$managedFlags = @("--spec-type", "--spec-draft-n-max", "--spec-draft-p-min")
for ($index = $argsList.Count - 2; $index -ge 0; $index--) {
    if ($managedFlags -contains $argsList[$index]) {
        $argsList.RemoveAt($index + 1)
        $argsList.RemoveAt($index)
    }
}

$argsList.Add("--spec-type")
$argsList.Add("draft-mtp")
$argsList.Add("--spec-draft-n-max")
$argsList.Add("3")
$argsList.Add("--spec-draft-p-min")
$argsList.Add("0.75")
$provider.llama_args = @($argsList)

$json = $config | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($providersPath, $json, [System.Text.UTF8Encoding]::new($false))

Write-Output "Configuration mise à jour : $providersPath"
Write-Output "Sauvegarde : $backupPath"
