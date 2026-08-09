export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function apiErrorDetail(payload: unknown, fallback: string): string {
  if (!isRecord(payload)) return fallback;
  if (typeof payload.detail === "string" && payload.detail.trim()) {
    return payload.detail;
  }
  if (Array.isArray(payload.detail)) {
    const messages = payload.detail.flatMap((entry) => {
      if (!isRecord(entry)) return [];
      const location = Array.isArray(entry.loc)
        ? entry.loc.filter((item) => item !== "body").join(" → ")
        : "";
      const message = typeof entry.msg === "string" ? entry.msg : "valeur invalide";
      return [`${location ? `${location} : ` : ""}${message}`];
    });
    if (messages.length) return messages.join(" · ");
  }
  return fallback;
}

/**
 * Met en forme le `detail` d'une réponse d'erreur.
 *
 * FastAPI renvoie une chaîne pour nos `HTTPException`, mais une LISTE d'objets
 * `{loc, msg, type}` quand c'est Pydantic qui refuse le corps de la requête.
 * Un `String()` direct sur cette liste produisait « [object Object] », ce qui
 * masquait complètement la cause du refus.
 */
export function formatApiDetail(detail: unknown): string {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (!isRecord(item)) return JSON.stringify(item);
        const field = Array.isArray(item.loc)
          // On retire le premier segment ("body", "query"…), sans intérêt ici.
          ? item.loc.slice(1).filter((part) => part !== "").join(".")
          : "";
        const message = typeof item.msg === "string" ? item.msg : JSON.stringify(item);
        return field ? `${field} : ${message}` : message;
      })
      .filter(Boolean)
      .join("\n");
  }
  if (isRecord(detail) && typeof detail.msg === "string") return detail.msg;
  return JSON.stringify(detail);
}

export async function readApiPayload<T = Record<string, unknown>>(
  response: Response,
): Promise<T> {
  const text = await response.text();
  let payload: unknown = {};
  if (text) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      if (!response.ok) {
        throw new Error(`Le serveur n’a pas pu traiter la demande (${response.status}).`);
      }
      throw new Error("Réponse serveur illisible.");
    }
  }
  if (!response.ok) {
    const detail =
      typeof payload === "object" && payload !== null && "detail" in payload
        ? (payload as { detail?: unknown }).detail
        : null;
    throw new Error(formatApiDetail(detail) || `La demande a échoué (${response.status}).`);
  }
  return payload as T;
}

// Fonction pure et sans dependances : definie au niveau module (et non inline
// dans le JSX) pour garder une identite stable entre les rendus. Sinon
// `FileExplorer` la voit changer a chaque rendu de `Home` et recharge son
// arborescence en boucle.
export function fileExplorerKernelUrl(path: string): string {
  return `/api/kernel${path.replace("/api", "")}`;
}
