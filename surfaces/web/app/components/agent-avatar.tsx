"use client";

import { useState } from "react";

/**
 * Avatar d'un agent, avec repli sur son initiale.
 *
 * `version` force le rechargement après un téléversement : le nom de fichier
 * servi ne change pas d'une image à l'autre, donc sans ce paramètre le
 * navigateur continuerait d'afficher l'ancienne.
 */
export function AgentAvatar({
  agentId,
  label,
  hasAvatar,
  version,
  className = "message-avatar",
}: {
  agentId?: string;
  label: string;
  hasAvatar?: boolean;
  version?: number;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const initial = label.slice(0, 1).toUpperCase() || "A";
  const showImage = Boolean(agentId && hasAvatar) && !failed;

  return (
    <div className={className} data-has-image={showImage ? "true" : undefined}>
      {showImage ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={`/api/kernel/admin/agents/${encodeURIComponent(agentId!)}/avatar${
            version ? `?v=${version}` : ""
          }`}
          alt=""
          onError={() => setFailed(true)}
        />
      ) : (
        initial
      )}
    </div>
  );
}
