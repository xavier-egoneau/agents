$ErrorActionPreference = "Stop"

$tag = "b10434"
$root = "C:\Users\egza_\Documents\content-agents"
$target = Join-Path $root "runtime\llama.cpp\$tag"
$temporary = Join-Path $env:TEMP "amk-llama-$tag"
$packages = @(
    @{
        Name = "llama-b10434-bin-win-cuda-13.3-x64.zip"
        Url = "https://github.com/ggml-org/llama.cpp/releases/download/b10434/llama-b10434-bin-win-cuda-13.3-x64.zip"
        Sha256 = "0162fa29e67a94c7422f5e2d2a56605fd0e4e95da8f2b6416f5927e6dc0aec99"
    },
    @{
        Name = "cudart-llama-bin-win-cuda-13.3-x64.zip"
        Url = "https://github.com/ggml-org/llama.cpp/releases/download/b10434/cudart-llama-bin-win-cuda-13.3-x64.zip"
        Sha256 = "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e"
    }
)

New-Item -ItemType Directory -Force -Path $temporary, $target | Out-Null
foreach ($package in $packages) {
    $archive = Join-Path $temporary $package.Name
    & curl.exe --fail --location --retry 10 --retry-all-errors --output $archive $package.Url
    if ($LASTEXITCODE -ne 0) {
        throw "Échec du téléchargement : $($package.Name)"
    }
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $package.Sha256) {
        throw "SHA-256 incorrect pour $($package.Name)"
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $target -Force
}

$server = Get-ChildItem $target -Filter "llama-server.exe" -File -Recurse | Select-Object -First 1
if (-not $server) {
    throw "llama-server.exe absent après extraction"
}
$serverPath = $server.FullName

$providersPath = Join-Path $root "providers.json"
$visionPath = Join-Path $root "vision.json"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Copy-Item $providersPath "$providersPath.before-llama-server-$stamp.bak"
Copy-Item $visionPath "$visionPath.before-llama-server-$stamp.bak"

$providers = Get-Content $providersPath -Raw | ConvertFrom-Json
foreach ($provider in $providers.providers) {
    if ($provider.kind -in @("llama-cpp", "llama.cpp")) {
        $provider.server_binary = $serverPath
    }
}
$providersTemporary = "$providersPath.tmp"
[System.IO.File]::WriteAllText(
    $providersTemporary,
    (($providers | ConvertTo-Json -Depth 30) + [Environment]::NewLine),
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item $providersTemporary $providersPath -Force

$vision = Get-Content $visionPath -Raw | ConvertFrom-Json
$vision.server_binary = $serverPath
$visionTemporary = "$visionPath.tmp"
[System.IO.File]::WriteAllText(
    $visionTemporary,
    (($vision | ConvertTo-Json -Depth 20) + [Environment]::NewLine),
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item $visionTemporary $visionPath -Force

& $serverPath --version
if ($LASTEXITCODE -ne 0) {
    throw "llama-server installé mais son test --version a échoué"
}
Write-Output "LLAMA_SERVER=$serverPath"
