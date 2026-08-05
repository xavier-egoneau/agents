"use client";

import type { CSSProperties } from "react";

/**
 * Signal de compaction en cours.
 *
 * La compaction résume le contexte via un appel au modèle : plusieurs secondes
 * pendant lesquelles la réponse ne progresse pas. Sans signal, l'attente est
 * indistinguable d'un blocage.
 *
 * Pas de barre de progression : le kernel ne connaît pas la durée de l'appel de
 * résumé, et une barre qui n'avance pas inquiète davantage que pas de barre.
 * Les pixels se tassent en boucle — ils disent « ça travaille », rien de plus.
 */
const PIXEL_COUNT = 18;

export function CompactionIndicator() {
  return (
    <div className="compaction-indicator" role="status">
      <span className="compaction-grid" aria-hidden="true">
        {Array.from({ length: PIXEL_COUNT }, (_, index) => (
          <i key={index} style={{ "--pixel": index } as CSSProperties} />
        ))}
      </span>
      <span className="compaction-label">Compaction du contexte…</span>
    </div>
  );
}
