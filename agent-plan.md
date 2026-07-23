La grille des outils indispensables pour un système agentique
Un système agentique n'est pas un monolithe. C'est une architecture en couches où chaque outil répond à un besoin précis : percevoir le monde, agir dessus, raisonner dans le temps, et collaborer. Voici les catégories fondamentales qui émergent des patterns observés en production.

1. Outils de perception et d'acquisition d'information
C'est la capacité la plus basique mais la plus critique : un agent doit pouvoir lire l'état du monde externe.

Web search et web scraping. L'agent doit pouvoir interroger le web en temps réel pour des faits, des actualités, de la documentation. Sans cela, il est confiné à sa fenêtre de contexte initiale. Les implémentations matures incluent un moteur de recherche structuré (avec pagination, filtrage par source, score de pertinence) et un scraper capable d'extraire du contenu textuel depuis des URL connues.

Lecture de fichiers et de bases de données. Un agent doit pouvoir inspecter le système de fichiers local, lire des fichiers texte, CSV, JSON, PDF, et interroger des bases SQL ou vectorielles. C'est le pont entre l'agent et les données persistantes de l'organisation.

Vision et traitement multimodal. Pour les agents qui opèrent dans un environnement visuel (interfaces utilisateur, diagrammes, documents scannés), des outils de capture d'écran, d'analyse d'image et d'OCR sont nécessaires. Le pattern "computer use" d'Anthropic en est l'illustration la plus aboutie.

2. Outils d'action et de modification du monde
Une fois que l'agent a perçu son environnement, il doit pouvoir le transformer.

Exécution de code (sandboxed). Un interpréteur sécurisé pour Python, JavaScript, ou tout autre langage permet à l'agent de calculer, transformer des données, générer des graphiques, et valider ses propres hypothèses. OpenAI appelle cela le code interpreter — un pattern universel. La sandbox est impérative pour la sécurité.

Écriture et manipulation de fichiers. La contrepartie de la lecture : créer, modifier, déplacer, supprimer des fichiers et des répertoires. Un agent qui ne peut pas écrire est un agent qui ne peut pas livrer.

Appels API et intégrations externes. Via le function calling (OpenAI) ou le Model Context Protocol (Anthropic), l'agent doit pouvoir invoquer des APIs REST, envoyer des emails, créer des tickets, mettre à jour des CRM, déclencher des pipelines CI/CD. C'est ici que le MCP prend tout son sens : il standardise l'interface entre l'agent et une bibliothèque extensible de serveurs d'outils.

Opérations sur le système (computer use). Pour les agents autonomes avancés, la capacité à contrôler un environnement bureautique — cliquer, taper, naviguer dans une interface graphique — ouvre des cas d'usage massifs (automatisation de workflows legacy, tests d'interface).

3. Outils de mémoire et de contexte
Un agent sans mémoire est un agent amnésique qui reçoit tout son contexte à chaque tour.

Mémoire à court terme (fenêtre de contexte). C'est la mémoire immédiate de la conversation en cours. Les systèmes agentiques doivent gérer le context window management : résumé automatique, rotation, et élagage pour ne pas exploser les limites de tokens.

Mémoire à long terme (RAG et bases vectorielles). Un système de retrieval augmenté (RAG) avec embeddings et base vectorielle (Chroma, Pinecone, Weaviate) permet à l'agent de consulter un historique illimité, une base de connaissances, ou des documents d'architecture. Le pattern "augmented LLM" d'Anthropic place le retrieval comme l'un des trois piliers avec les tools et la mémoire.

Mémoire épisodique et sémantique. Au-delà du RAG brut, des implémentations plus sophistiquées distinguent la mémoire des faits (sémantique) de la mémoire des expériences passées (épisodique) — ce qu'un agent a déjà tenté, ce qui a fonctionné ou échoué. Des frameworks comme ceux de LangChain commencent à explorer cette distinction.

4. Outils de planification et de raisonnement
C'est le cerveau exécutif de l'agent.

Décomposition de tâches (task planner). Un outil qui permet à l'agent de découper un objectif complexe en sous-tâches ordonnées. Le pattern orchestrator-workers d'Anthropic en est l'exemple canonique : un LLM central décompose dynamiquement le travail et délègue à des workers spécialisés.

Boucle évaluateur-optimiseur. Un outil de méta-cognition où un premier module génère une solution et un second l'évalue, en boucle itérative, jusqu'à satisfaction d'un critère. Utilisé massivement en traduction, révision de code, génération de contenu.

Routing et classification. Un routeur qui analyse l'entrée utilisateur et la dirige vers le bon spécialiste (LLM spécialisé, workflow prédéfini, outil spécifique). C'est le pattern de base de toute architecture orientée services.

Validation et guardrails. Des outils de filtrage, de validation de schéma, de détection de contenu inapproprié, et de vérification des préconditions avant exécution d'actions critiques. Le pattern "parallelization with guardrails" (Anthropic) montre qu'il est plus fiable d'avoir un modèle dédié à la modération qu'un même modèle qui fait tout.

5. Outils d'orchestration et de flux de contrôle
L'agent n'opère pas dans le vide ; il doit s'intégrer dans un système plus large.

Prompt chaining. L'enchaînement séquentiel de plusieurs appels LLM où la sortie de l'un est l'entrée du suivant, avec des points de vérification programmatiques entre chaque étape. C'est le workflow le plus simple et le plus fiable pour les tâches décomposables.

Parallelisation. La capacité d'exécuter des sous-tâches en parallèle (sectioning) ou de lancer plusieurs tentatives d'une même tâche pour voter (voting) sur la meilleure réponse.

Boucle agent (agent loop). Le cœur de tout système agentique : une boucle percevoir → raisonner → agir → observer qui s'exécute jusqu'à terminaison ou intervention humaine. La boucle doit gérer les erreurs, les timeouts, les limites d'itérations, et les points de pause pour validation humaine.

Human-in-the-loop (HITL). Des points de contrôle où l'agent s'arrête et sollicite une décision ou une validation humaine. Indispensable pour la confiance et la sécurité dans les environnements de production.

6. Outils de persistance et d'état
Un agent doit survivre aux redémarrages et aux pannes.

Session management. La capacité de sauvegarder et restaurer l'état complet d'une session agent (contexte, historique, résultats intermédiaires).

État durable (durable execution). LangChain/LangGraph appelle cela la durable execution : la capacité de reprendre un workflow agent exactement là où il s'est arrêté, même après un crash. Crucial pour les tâches longues.

Checkpointing et reprise. Des sauvegardes périodiques de l'état pour permettre la reprise après échec sans tout recommencer.

7. Outils de découverte et de composition dynamique
L'avenir des systèmes agentiques est dans la composition dynamique d'outils.

Tool search / découverte différée. OpenAPI récemment introduit le tool search : l'agent ne charge pas tous les outils au départ, mais découvre et charge les outils pertinents à la volée en fonction de la tâche. C'est indispensable quand la bibliothèque d'outils dépasse quelques dizaines.

MCP (Model Context Protocol). Le protocole standardisé d'Anthropic permet à un agent de découvrir dynamiquement les outils disponibles sur un serveur MCP, d'inspecter leurs schémas d'entrée/sortie, et de les invoquer de manière uniforme. C'est en train de devenir le standard de facto pour l'interopérabilité des outils agentiques.

Namespaces et catégorisation. La capacité de regrouper les outils par domaine (CRM, billing, shipping) et de permettre à l'agent de naviguer dans cette arborescence pour trouver l'outil approprié.

8. Outils de monitoring et d'observabilité
Un agent en production sans observabilité est une boîte noire dangereuse.

Tracing et debugging. Des outils comme LangSmith, Arize, ou Helicone permettent de tracer chaque étape de raisonnement, chaque appel d'outil, chaque latence. C'est ce qui rend un système agentique débogable.

Métriques de performance. Taux de succès des tool calls, nombre d'itérations avant complétion, coût par tâche, latence. Ces métriques sont nécessaires pour justifier le passage en production.

Logging et audit trail. Chaque action de l'agent doit être horodatée, signée, et persistée pour conformité et révision.

Synthèse : l'architecture en couches
Si l'on devait schématiser le système agentique idéal, il ressemblerait à ceci :

Couche 1 — Fondation : LLM augmenté (retrieval + tools + mémoire) via une API standardisée (MCP ou function calling).

Couche 2 — Outils de base : web search, filesystem, code execution, API calls. Ces quatre outils sont le minimum vital pour qu'un agent ait une utilité concrète.

Couche 3 — Mémoire : court terme (context management) + long terme (RAG vectoriel) + épisodique (historique des actions).

Couche 4 — Raisonnement : task planner, evaluator-optimizer, router, guardrails.

Couche 5 — Orchestration : agent loop, prompt chaining, parallelisation, HITL.

Couche 6 — Observabilité : tracing, logging, métriques.

La leçon d'Anthropic et d'OpenAI est unanime : les systèmes agentiques les plus efficaces en production ne sont pas ceux qui utilisent les frameworks les plus complexes, mais ceux qui construisent avec des patterns simples, composables, et dont chaque outil est soigneusement documenté. La simplicité et la transparence du raisonnement — montrer explicitement les étapes de planification de l'agent — sont les deux piliers qui font la différence entre une démonstration et un système fiable.