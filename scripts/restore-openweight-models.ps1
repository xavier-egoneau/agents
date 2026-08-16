$ErrorActionPreference = "Stop"

$root = "C:\Users\egza_\Documents\content-agents\models"
$downloads = @(
    @{
        Directory = "qwen-27b"
        Name = "qwen3.6-27b-ud-q4kxl.gguf"
        Size = 17612564704
        Url = "https://huggingface.co/unsloth/Qwen3.6-27B-GGUF/resolve/main/Qwen3.6-27B-UD-Q4_K_XL.gguf"
    },
    @{
        Directory = "qwen-35b"
        Name = "qwen3.6-35b-a3b-ud-iq4nl.gguf"
        Size = 18040888288
        Url = "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-IQ4_NL.gguf"
    },
    @{
        Directory = "qwen-35b"
        Name = "qwen3.6-35b-a3b-ud-q8kxl.gguf"
        Size = 38451182560
        Url = "https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q8_K_XL.gguf"
    },
    @{
        Directory = "drafts"
        Name = "Qwen3.6-27B-DFlash-Q8_0.gguf"
        Size = 1849481440
        Url = "https://huggingface.co/ggml-org/Qwen3.6-27B-GGUF/resolve/main/dflash-Qwen3.6-27B-Q8_0.gguf"
    },
    @{
        Directory = "vision"
        Name = "gemma-4-E2B-it-Q4_K_M.gguf"
        Size = 3106738272
        Url = "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_K_M.gguf"
    },
    @{
        Directory = "vision"
        Name = "mmproj-gemma-4-E2B-it-BF16.gguf"
        Size = 986833664
        Url = "https://huggingface.co/ggml-org/gemma-4-E2B-it-GGUF/resolve/main/mmproj-gemma-4-E2B-it-BF16.gguf"
    },
    @{
        Directory = "qwen-27b"
        Name = "qwen3.8-27b-ud-q4kxl.gguf"
        Size = 17923394624
        Url = "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q4_K_XL.gguf"
    },
    @{
        Directory = "qwen-27b"
        Name = "qwen3.8-27b-ud-q3kxl.gguf"
        Size = 13441059904
        Url = "https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q3_K_XL.gguf"
    }
)

foreach ($download in $downloads) {
    $directory = Join-Path $root $download.Directory
    $target = Join-Path $directory $download.Name
    $partial = "$target.part"
    New-Item -ItemType Directory -Force -Path $directory | Out-Null

    if ((Test-Path -LiteralPath $target) -and
        (Get-Item -LiteralPath $target).Length -eq $download.Size) {
        Write-Output "Déjà présent : $target"
        continue
    }

    # Les URLs Hugging Face/Xet peuvent ignorer Range après une redirection.
    # curl concaténerait alors le fichier complet à un fragment existant.
    if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
    }

    Write-Output "Téléchargement : $($download.Name)"
    & curl.exe --fail --location --retry 10 --retry-all-errors `
        --output $partial $download.Url
    if ($LASTEXITCODE -ne 0) {
        throw "Échec du téléchargement : $($download.Name)"
    }
    if ((Get-Item -LiteralPath $partial).Length -ne $download.Size) {
        throw "Taille incorrecte après téléchargement : $($download.Name)"
    }
    Move-Item -LiteralPath $partial -Destination $target -Force
    Write-Output "Terminé : $target"
}

$contentRoot = "C:\Users\egza_\Documents\content-agents"
$providersPath = Join-Path $contentRoot "providers.json"
$visionPath = Join-Path $contentRoot "vision.json"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Copy-Item -LiteralPath $providersPath -Destination "$providersPath.before-model-restore-$stamp.bak"
$providers = Get-Content -LiteralPath $providersPath -Raw
$providers = $providers.Replace(
    "C:\\Users\\egza_\\llama.ccp\\models-moe-fast",
    "C:\\Users\\egza_\\Documents\\content-agents\\models\\qwen-35b"
)
$providers = $providers.Replace(
    "C:\\Users\\egza_\\llama.ccp\\models-moe",
    "C:\\Users\\egza_\\Documents\\content-agents\\models\\qwen-35b"
)
$providers = $providers.Replace(
    "C:\\Users\\egza_\\llama.ccp\\models",
    "C:\\Users\\egza_\\Documents\\content-agents\\models\\qwen-27b"
)
$providers = $providers.Replace(
    "C:/Users/egza_/llama.ccp/models/drafts/Qwen3.6-27B-DFlash-Q8_0.gguf",
    "C:/Users/egza_/Documents/content-agents/models/drafts/Qwen3.6-27B-DFlash-Q8_0.gguf"
)
[System.IO.File]::WriteAllText($providersPath, $providers, [System.Text.UTF8Encoding]::new($false))

Copy-Item -LiteralPath $visionPath -Destination "$visionPath.before-model-restore-$stamp.bak"
$vision = Get-Content -LiteralPath $visionPath -Raw
$vision = $vision.Replace(
    "C:\\Users\\egza_\\.lmstudio\\models\\lmstudio-community\\gemma-4-E2B-it-GGUF\\gemma-4-E2B-it-Q4_K_M.gguf",
    "C:\\Users\\egza_\\Documents\\content-agents\\models\\vision\\gemma-4-E2B-it-Q4_K_M.gguf"
)
$vision = $vision.Replace(
    "C:\\Users\\egza_\\.lmstudio\\models\\lmstudio-community\\gemma-4-E2B-it-GGUF\\mmproj-gemma-4-E2B-it-BF16.gguf",
    "C:\\Users\\egza_\\Documents\\content-agents\\models\\vision\\mmproj-gemma-4-E2B-it-BF16.gguf"
)
[System.IO.File]::WriteAllText($visionPath, $vision, [System.Text.UTF8Encoding]::new($false))

Set-Content -LiteralPath (Join-Path $root "RESTORE-COMPLETE.txt") `
    -Value "Restauration terminée le $(Get-Date -Format o)" -Encoding UTF8
Write-Output "Tous les modèles et leurs chemins de configuration ont été restaurés."
