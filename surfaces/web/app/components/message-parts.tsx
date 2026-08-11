"use client";

/**
 * RENDU D'UN MESSAGE
 * -----------------------------------------------------------------------------
 * Les quatre morceaux qui composent une bulle de conversation : la vitesse du
 * run, le markdown, les artefacts produits et les images jointes.
 *
 * Tous prennent leurs données en propriétés et ne tiennent aucun état. Les
 * sortir de `page.tsx` ne change donc rien à leur comportement, et rend leur
 * rendu lisible sans dérouler quatre mille lignes.
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { RunStats } from "../lib/trace";
import type { ComposerImage, ImagePreview, RunArtifact } from "../lib/model";

function secondes(ms: number): string {
  const valeur = ms / 1000;
  return valeur < 10 ? `${valeur.toFixed(1)} s` : `${Math.round(valeur)} s`;
}

function nombre(tokens: number): string {
  return tokens.toLocaleString("fr-FR");
}

/**
 * Ce qu'a coûté un run, en faits vérifiables.
 *
 * Volontairement pas une vitesse : le modèle lit tout le prompt avant d'écrire
 * le premier token, et cette lecture domine largement les réponses courtes.
 * Diviser la réponse par la durée totale produisait un chiffre qui s'effondrait
 * quand le prompt grossissait, sans que rien ne ralentisse.
 */
export function RunCost({ stats, runId }: { stats: Map<string, RunStats>; runId?: string }) {
  const entry = runId ? stats.get(runId) : undefined;
  if (!entry || entry.durationMs <= 0 || entry.outputTokens <= 0) return null;
  const attente = entry.toolMs + entry.waitMs;
  const detail: string[] = [
    `Prompt envoyé au modèle : ${nombre(entry.inputTokens)} tokens.`,
    `Réponse écrite : ${nombre(entry.outputTokens)} tokens.`,
    `Durée totale : ${secondes(entry.durationMs)}.`,
  ];
  if (entry.toolMs > 0) detail.push(`Dont outils : ${secondes(entry.toolMs)}.`);
  if (entry.waitMs > 0) detail.push(`Dont attente d'autorisation : ${secondes(entry.waitMs)}.`);
  if (entry.prefillPerSecond) {
    detail.push(`Lecture du prompt : ${Math.round(entry.prefillPerSecond)} tokens/s.`);
  }
  if (entry.generationPerSecond) {
    detail.push(
      `Écriture : ${Math.round(entry.generationPerSecond)} tokens/s, chronométrée par le `
      + "serveur d'inférence.",
    );
  } else {
    detail.push(
      "Le modèle lit tout le prompt avant d'écrire : sur une réponse courte, "
      + "cette durée mesure surtout la taille du prompt.",
    );
  }
  return (
    <small className="message-speed" title={detail.join("\n")}>
      {nombre(entry.outputTokens)} tokens · {secondes(entry.durationMs)}
      {entry.generationPerSecond
        ? ` · ${Math.round(entry.generationPerSecond)} tokens/s en écriture`
        : attente > 0 && ` (dont ${secondes(attente)} hors modèle)`}
    </small>
  );
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children, ...props }) => (
            <a
              {...props}
              href={href}
              target={href?.startsWith("http") ? "_blank" : undefined}
              rel={href?.startsWith("http") ? "noreferrer noopener" : undefined}
            >
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export function MessageArtifacts({
  sessionId, artifacts, onImageOpen,
}: {
  sessionId: string | null;
  artifacts?: RunArtifact[];
  onImageOpen: (image: ImagePreview) => void;
}) {
  const visibleArtifacts = artifacts?.filter((artifact) => artifact.kind !== "input_image");
  if (!sessionId || !visibleArtifacts?.length) return null;
  return (
    <div className="message-artifacts">
      {visibleArtifacts.map((artifact) => {
        const source = `/api/kernel/artifacts/${encodeURIComponent(sessionId)}/${encodeURIComponent(artifact.artifact_id)}`;
        return artifact.kind === "image" || artifact.media_type.startsWith("image/") ? (
          <figure key={artifact.artifact_id}>
            <button
              type="button"
              className="message-image-button"
              aria-label={`Agrandir ${artifact.name}`}
              onClick={() => onImageOpen({ source, name: artifact.name })}
            >
              {/* Runtime artifact URLs are not statically optimizable by Next Image. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={source} alt={artifact.name} loading="lazy" />
            </button>
            <figcaption>{artifact.name}</figcaption>
          </figure>
        ) : (
          <a key={artifact.artifact_id} href={source} download={artifact.name}>
            {artifact.name}
          </a>
        );
      })}
    </div>
  );
}

export function UserMessageImages({
  images, onImageOpen,
}: {
  images?: ComposerImage[];
  onImageOpen: (image: ImagePreview) => void;
}) {
  if (!images?.length) return null;
  return (
    <div className="user-message-images" aria-label="Images envoyées">
      {images.map((image) => (
        <figure key={image.id}>
          <button
            type="button"
            className="message-image-button"
            aria-label={`Agrandir ${image.name}`}
            onClick={() => onImageOpen({ source: image.dataUrl, name: image.name })}
          >
            {/* User-provided data URLs cannot be handled by Next Image. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={image.dataUrl} alt={image.name} />
          </button>
          <figcaption>{image.name}</figcaption>
        </figure>
      ))}
    </div>
  );
}
