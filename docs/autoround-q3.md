# Quantification AutoRound Q3

Le script `scripts/quantize-autoround-q3.ps1` fabrique un GGUF Q3 calibré avec
AutoRound, destiné au provider `llama.cpp` existant. Il utilise un environnement
Python isolé sous `content-agents/runtime/autoround` et ne change jamais le
modèle actif.

Sous Windows, le script installe explicitement PyTorch CUDA 13.0 depuis l'index
officiel PyTorch ; la roue PyPI générique est une version CPU inutilisable ici.

La recette par défaut cible `unsloth/Qwen3.8-27B`, exporte en `Q3_K_L` et utilise
128 échantillons de 2 048 tokens avec 200 itérations. Le batch de 1, l'accumulation
de gradients et les modes basse mémoire sont adaptés à une RTX 3090 de 24 Go.

```powershell
# Vérifier exactement ce qui sera fait (aucun téléchargement)
.\scripts\quantize-autoround-q3.ps1 -Action Plan

# Créer l'environnement isolé et installer AutoRound
.\scripts\quantize-autoround-q3.ps1 -Action Install

# Télécharger le modèle source, calibrer et exporter le GGUF
.\scripts\quantize-autoround-q3.ps1 -Action Quantize
```

Le téléchargement du checkpoint source et son cache demandent beaucoup
d'espace. Le script exige au moins 80 Go libres avant de commencer. Les fichiers
intermédiaires restent conservés pour diagnostic ou reprise manuelle. Une fois
le GGUF produit, il est copié dans `content-agents/models/qwen-27b` sans modifier
`providers.json`.

`Q3_K_XL` est une recette dynamique propre à certains exports Unsloth et ne fait
pas partie des formats GGUF standards proposés par AutoRound. `Q3_K_L` constitue
donc le candidat AutoRound le plus proche à comparer au modèle actuel.

Pour tester une autre source ou une calibration plus courte :

```powershell
.\scripts\quantize-autoround-q3.ps1 -Action Quantize `
  -Model "unsloth/Qwen3.6-27B" -Iterations 100 -Samples 64
```

La comparaison doit porter au minimum sur les tâches agentiques habituelles, le
tool calling, la stabilité en contexte long, le débit, la VRAM et la taille du
fichier. Le modèle actif ne devrait être changé qu'après ce benchmark.
