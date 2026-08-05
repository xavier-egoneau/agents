"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Icon } from "../../theme/theme-context";
import type { IconName } from "../../theme/icons";
import { ChromeEmpty } from "./shell";

type Node = { name: string; path: string; kind: "dir" | "file"; size: number | null };

type TreeResponse = { path: string; entries: Node[]; truncated: boolean };

/** Etat par dossier : evite de recharger un niveau deja parcouru. */
type Level = { entries: Node[]; loading: boolean; error?: string };

const EXTENSION_ICONS: Record<string, IconName> = {
  ts: "fileCode", tsx: "fileCode", js: "fileCode", jsx: "fileCode", mjs: "fileCode",
  py: "fileCode", rs: "fileCode", go: "fileCode", sh: "terminal",
  json: "json", yaml: "json", yml: "json", toml: "json",
  png: "image", jpg: "image", jpeg: "image", svg: "image", webp: "image", gif: "image",
};

function iconFor(node: Node, expanded: boolean): IconName {
  if (node.kind === "dir") return expanded ? "folderOpen" : "folder";
  const extension = node.name.split(".").pop()?.toLowerCase();
  return (extension && EXTENSION_ICONS[extension]) || "file";
}

function formatSize(bytes: number | null) {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} o`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} ko`;
  return `${(bytes / 1024 / 1024).toFixed(1)} Mo`;
}

/**
 * Explorateur en lecture seule. Chaque dossier est charge a l'expansion, ce qui
 * evite de rapatrier des arborescences entieres sur les gros depots.
 */
export function FileExplorer({
  workspace,
  kernelUrl,
  changedPaths,
  onOpenFile,
  selectedPath,
}: {
  workspace?: string;
  kernelUrl: (path: string) => string;
  changedPaths?: Map<string, string>;
  onOpenFile: (path: string) => void;
  selectedPath?: string;
}) {
  const [levels, setLevels] = useState<Record<string, Level>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");

  const load = useCallback(
    async (path: string) => {
      if (!workspace) return;
      setLevels((current) => ({ ...current, [path]: { entries: current[path]?.entries ?? [], loading: true } }));
      try {
        const query = new URLSearchParams({ workspace, path });
        const response = await fetch(kernelUrl(`/api/files/tree?${query}`));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = (await response.json()) as TreeResponse;
        setLevels((current) => ({ ...current, [path]: { entries: data.entries, loading: false } }));
      } catch (error) {
        setLevels((current) => ({
          ...current,
          [path]: { entries: [], loading: false, error: error instanceof Error ? error.message : "Erreur" },
        }));
      }
    },
    [workspace, kernelUrl],
  );

  // `load` change d'identite a chaque rendu du parent (kernelUrl est souvent
  // recree inline). On passe par une ref pour ne re-declencher le chargement
  // que sur un vrai changement de workspace, sinon l'arbre se reinitialise et
  // clignote a chaque re-rendu du composant parent.
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  // Recharge la racine des qu'on change de workspace.
  useEffect(() => {
    setLevels({});
    setExpanded(new Set());
    if (workspace) void loadRef.current("");
  }, [workspace]);

  const toggle = useCallback(
    (node: Node) => {
      if (node.kind === "file") {
        onOpenFile(node.path);
        return;
      }
      setExpanded((current) => {
        const next = new Set(current);
        if (next.has(node.path)) {
          next.delete(node.path);
        } else {
          next.add(node.path);
          if (!levels[node.path]) void load(node.path);
        }
        return next;
      });
    },
    [levels, load, onOpenFile],
  );

  /** Aplatit l'arbre visible en une liste, avec la profondeur pour l'indentation. */
  const rows = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const output: Array<{ node: Node; depth: number }> = [];

    const walk = (path: string, depth: number) => {
      const level = levels[path];
      if (!level) return;
      for (const node of level.entries) {
        const matches = !needle || node.name.toLowerCase().includes(needle);
        const isOpen = expanded.has(node.path);
        if (matches) output.push({ node, depth });
        if (node.kind === "dir" && isOpen) walk(node.path, depth + 1);
      }
    };

    walk("", 0);
    return output;
  }, [levels, expanded, filter]);

  if (!workspace) {
    return <ChromeEmpty icon="folder">Selectionnez un projet pour explorer ses fichiers.</ChromeEmpty>;
  }

  const root = levels[""];

  return (
    <div className="file-explorer">
      <div className="file-explorer-toolbar">
        <input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filtrer par nom…"
          aria-label="Filtrer les fichiers"
        />
        <button
          type="button"
          className="ibtn sm"
          data-tip="Recharger"
          data-tip-side="left"
          aria-label="Recharger l'arborescence"
          onClick={() => {
            setLevels({});
            setExpanded(new Set());
            void load("");
          }}
        >
          <Icon name="refresh" size="sm" />
        </button>
      </div>

      <div className="file-tree" role="tree">
        {root?.loading && !root.entries.length && <ChromeEmpty icon="running" spin>Chargement…</ChromeEmpty>}
        {root?.error && <ChromeEmpty icon="error">{root.error}</ChromeEmpty>}
        {!root?.loading && !root?.error && !rows.length && (
          <ChromeEmpty icon="search">Aucun fichier ne correspond.</ChromeEmpty>
        )}

        {rows.map(({ node, depth }) => {
          const isOpen = expanded.has(node.path);
          return (
            <button
              key={node.path}
              type="button"
              role="treeitem"
              className="file-node"
              style={{ "--depth": depth } as CSSProperties}
              aria-expanded={node.kind === "dir" ? isOpen : undefined}
              // `aria-selected` est requis par le rôle treeitem, et remplace
              // `aria-current` qui n'a pas de sens dans une arborescence.
              aria-selected={selectedPath === node.path}
              data-status={changedPaths?.get(node.path)}
              onClick={() => toggle(node)}
              title={node.path}
            >
              {node.kind === "dir" ? (
                <Icon name="chevronRight" size="xs" className="twist" />
              ) : (
                <span style={{ width: "var(--icon-size-xs)" }} />
              )}
              <Icon name={iconFor(node, isOpen)} size="sm" />
              <span>{node.name}</span>
              <em>{node.kind === "file" ? formatSize(node.size) : ""}</em>
            </button>
          );
        })}
      </div>
    </div>
  );
}
