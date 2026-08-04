# Format `amk.workflow/v1`

Utiliser ce contrat comme source de vérité lisible et validable d’une skill de
routine. Il décrit un workflow guidé par le modèle ; il n’exécute rien et
n’accorde aucune permission.

## Structure

```yaml
schema: amk.workflow/v1
id: routine-veille-matin-v1
title: Veille du matin
status: ready

execution:
  mode: agent_guided
  deviation: stop_and_report
  timezone: Europe/Paris

parameters:
  news_query:
    type: string
    default: actualité

variables:
  today:
    from: runtime.local_date

permissions:
  authority: kernel_guardian
  unlisted: stop_and_report
  declarations:
    - tool: web_search
      fixed_args:
        scrape_results: false

missing_dependencies: []

steps:
  - id: news
    kind: tool
    tool: web_search
    args:
      query: "${parameters.news_query} ${variables.today}"
      limit: 8
      scrape_results: false
      justification: Rechercher les actualités du jour pour la veille.
    save_as: news_results
    retry:
      attempts: 2
      on: [retryable]
    fallback:
      action: fail_partial
    evidence:
      required: true
      capture: [data]

  - id: synthesis
    kind: synthesize
    needs: [news]
    instructions: >
      Résumer uniquement les informations soutenues par les résultats capturés.
    evidence:
      from_steps: [news]

output:
  format: markdown
  language: fr
  sections: [Actualités importantes, À retenir]
  evidence_policy: cite_or_mark_uncertain
  on_incomplete: partial_with_warnings
```

## Champs

- `status` : utiliser `draft`, `ready` ou `blocked`. Un workflow `ready` ne doit
  avoir aucune dépendance manquante. Un workflow `blocked` doit les documenter.
- `execution.mode` : conserver `agent_guided` tant qu’AMK ne fournit pas de
  runner strict.
- `execution.deviation` : utiliser `stop_and_report` pour empêcher les replis
  improvisés ; utiliser `allow_declared_fallbacks` seulement lorsque chaque
  fallback est écrit dans les étapes.
- `parameters` : déclarer les entrées configurables avec un type et une valeur
  par défaut.
- `variables` : dériver les valeurs d’exécution, par exemple
  `runtime.local_date`, `runtime.now`, `runtime.timezone` ou
  `runtime.scheduled_for`.
- `permissions.declarations` : lister chaque outil outillé. `fixed_args` fige
  les paramètres qui déterminent la frontière d’usage. Le Guardian reste la
  seule autorité d’autorisation.
- Le `SKILL.md` compagnon doit reprendre `read` et ces outils réels dans son
  frontmatter `allowed-tools`. Cette liste rend le contrat visible à l’agent ;
  elle ne remplace ni la validation du catalogue ni le Guardian.
- `missing_dependencies` : lister `capability` et `reason`. Ne jamais mettre un
  faux outil dans `steps` pour contourner une dépendance absente.
- `steps[].needs` : exprimer les dépendances explicitement. Les identifiants
  doivent être uniques et le graphe sans cycle.
- `steps[].kind` : utiliser `tool` ou `synthesize`.
- `steps[].args` : respecter exactement le schéma retourné par
  `tool_describe`. Utiliser `${parameters.nom}`, `${variables.nom}` ou
  `${steps.identifiant...}` pour les valeurs différées.
- `steps[].retry.attempts` : utiliser une valeur de 1 à 3.
- `steps[].fallback.action` : utiliser `stop`, `continue` ou `fail_partial`.
- `steps[].evidence` : capturer les données nécessaires pour vérifier le
  résultat et alimenter la synthèse.
- `output` : fixer format, langue, sections, politique de preuve et comportement
  en cas de données incomplètes.

## Exemple avec agenda absent

Si aucun outil calendrier n’apparaît dans le catalogue, ne pas écrire
`tool: get_agenda`. Produire plutôt :

```yaml
status: blocked
missing_dependencies:
  - capability: calendar.events.list
    reason: Aucun connecteur calendrier ou outil MCP correspondant n’est configuré.
```

Présenter ensuite la dépendance à connecter. Ne passer à `ready` qu’après avoir
inspecté et figé le vrai nom d’outil et ses vrais paramètres.
