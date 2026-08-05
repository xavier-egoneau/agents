/**
 * Types de l'exécution Cloudflare utilisés par `worker/index.ts`.
 *
 * Ils viendraient normalement de `@cloudflare/workers-types`, absent des
 * dépendances, ou d'un `worker-configuration.d.ts` généré par `wrangler types`
 * — impossible ici : la configuration Cloudflare vit dans `vite.config.ts` via
 * le plugin Vite, et `wrangler` ne trouve donc aucun fichier à lire.
 *
 * Ces déclarations couvrent strictement ce que le point d'entrée manipule. Le
 * jour où `@cloudflare/workers-types` est installé, ce fichier disparaît.
 */

/** Liaison de service Cloudflare : seul `fetch` est utilisé ici. */
interface Fetcher {
  fetch(input: Request | string, init?: RequestInit): Promise<Response>;
}

/**
 * Base D1. Déclarée comme type opaque : `worker/index.ts` ne fait que la
 * transporter dans `Env`, sans jamais l'interroger. Lui inventer une surface
 * d'API laisserait croire qu'elle a été vérifiée.
 */
type D1Database = unknown;
