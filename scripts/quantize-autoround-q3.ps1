[CmdletBinding()]
param(
    [ValidateSet("Plan", "Install", "Quantize")]
    [string]$Action = "Plan",
    [string]$Model = "unsloth/Qwen3.8-27B",
    [ValidateSet("q3_k_s", "q3_k_m", "q3_k_l")]
    [string]$Format = "q3_k_l",
    [int]$Iterations = 200,
    [int]$Samples = 128,
    [int]$SequenceLength = 2048,
    [string]$Dataset = "NeelNanda/pile-10k:concat=True",
    [string]$ContentRoot = (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "content-agents"),
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$autoRoundVersion = "0.13.1"
$torchVersion = "2.13.0+cu130"
$torchIndex = "https://download.pytorch.org/whl/cu130"
$runtimeRoot = Join-Path $ContentRoot "runtime\autoround"
$venvRoot = Join-Path $runtimeRoot ".venv"
$python = Join-Path $venvRoot "Scripts\python.exe"
$autoRound = Join-Path $venvRoot "Scripts\auto-round.exe"
$workRoot = Join-Path $runtimeRoot "work"
$cacheRoot = Join-Path $runtimeRoot "huggingface-cache"
$outputRoot = Join-Path $workRoot ("{0}-{1}" -f (($Model -split "/")[-1]), $Format)
$modelsRoot = Join-Path $ContentRoot "models\qwen-27b"
$target = Join-Path $modelsRoot ("{0}-autoround-{1}.gguf" -f (($Model -split "/")[-1]).ToLowerInvariant(), $Format)

function Show-Plan {
    Write-Output "AutoRound Q3 plan"
    Write-Output "  Model source : $Model"
    Write-Output "  Export       : GGUF $($Format.ToUpperInvariant()) (calibrated W3A16)"
    Write-Output "  Calibration  : $Samples samples, $Iterations iterations, $SequenceLength tokens"
    Write-Output "  Dataset      : $Dataset"
    Write-Output "  Environment  : $venvRoot (auto-round $autoRoundVersion)"
    Write-Output "  Cache/work   : $runtimeRoot"
    Write-Output "  Final model  : $target"
    Write-Output "  Active model : unchanged"
}

function Assert-Cuda {
    if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
        throw "nvidia-smi introuvable : une carte NVIDIA CUDA est requise pour cette recette."
    }
    & $python -c "import torch; assert torch.cuda.is_available(), 'PyTorch ne voit pas CUDA'; print(torch.__version__, torch.cuda.get_device_name(0))"
    if ($LASTEXITCODE -ne 0) {
        throw "L'environnement AutoRound n'a pas de PyTorch CUDA fonctionnel."
    }
}

Show-Plan
if ($Action -eq "Plan") {
    Write-Output "Plan seulement : aucun téléchargement ni changement effectué."
    exit 0
}

if ($Action -eq "Install") {
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($null -eq $uv) {
        throw "uv est requis pour créer l'environnement AutoRound isolé."
    }
    New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
    if (-not (Test-Path -LiteralPath $python)) {
        & uv venv --python 3.12 $venvRoot
        if ($LASTEXITCODE -ne 0) { throw "Échec de création de l'environnement AutoRound." }
    }
    # PyPI fournit une roue PyTorch CPU sous Windows. Installer explicitement
    # la roue CUDA avant AutoRound empêche le résolveur de retenir cette variante.
    & uv pip install --python $python --index-url $torchIndex "torch==$torchVersion"
    if ($LASTEXITCODE -ne 0) { throw "Échec d'installation de PyTorch CUDA." }
    & uv pip install --python $python "auto-round==$autoRoundVersion"
    if ($LASTEXITCODE -ne 0) { throw "Échec d'installation d'AutoRound." }
    Assert-Cuda
    & $autoRound list format
    if ($LASTEXITCODE -ne 0) { throw "AutoRound installé, mais la vérification des formats a échoué." }
    Write-Output "Installation AutoRound validée. Lancez ensuite avec -Action Quantize."
    exit 0
}

if (-not (Test-Path -LiteralPath $autoRound)) {
    throw "AutoRound n'est pas installé. Lancez d'abord ce script avec -Action Install."
}
Assert-Cuda

if ((Test-Path -LiteralPath $target) -and -not $Force) {
    throw "Le modèle cible existe déjà : $target. Utilisez -Force pour le remplacer."
}

$drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($runtimeRoot))
$minimumFree = 80GB
if ($drive.AvailableFreeSpace -lt $minimumFree) {
    $freeGB = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
    throw "Espace libre insuffisant : $freeGB Go disponibles, 80 Go minimum recommandés."
}

New-Item -ItemType Directory -Force -Path $workRoot, $cacheRoot, $modelsRoot | Out-Null
if (Test-Path -LiteralPath $outputRoot) {
    throw "Le répertoire de travail existe déjà : $outputRoot. Déplacez-le ou supprimez-le avant de reprendre."
}

$env:HF_HOME = $cacheRoot
$env:HF_HUB_ENABLE_HF_TRANSFER = "0"
$arguments = @(
    "--model", $Model,
    "--scheme", "W3A16",
    "--format", "gguf:$Format",
    "--output_dir", $outputRoot,
    "--dataset", $Dataset,
    "--iters", "$Iterations",
    "--nsamples", "$Samples",
    "--seqlen", "$SequenceLength",
    "--bs", "1",
    "--gradient_accumulate_steps", "8",
    "--low_gpu_mem_usage",
    "--low_cpu_mem_usage"
)

Write-Output "Démarrage de la quantification. Le téléchargement et le calcul peuvent durer plusieurs heures."
& $autoRound @arguments
if ($LASTEXITCODE -ne 0) {
    throw "AutoRound a échoué. Les données de reprise sont conservées dans $runtimeRoot."
}

$generated = Get-ChildItem -LiteralPath $outputRoot -Filter "*.gguf" -File -Recurse |
    Sort-Object Length -Descending | Select-Object -First 1
if ($null -eq $generated -or $generated.Length -lt 1GB) {
    throw "AutoRound a terminé sans produire de GGUF plausible dans $outputRoot."
}
if ((Test-Path -LiteralPath $target) -and $Force) {
    $backup = "$target.before-autoround-$(Get-Date -Format 'yyyyMMdd-HHmmss').bak"
    Move-Item -LiteralPath $target -Destination $backup
    Write-Output "Ancien modèle sauvegardé : $backup"
}
Copy-Item -LiteralPath $generated.FullName -Destination $target
Write-Output "Modèle AutoRound prêt : $target"
Write-Output "Le provider actif n'a pas été modifié."
