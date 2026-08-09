import { useCallback, useEffect, useState } from "react";
import { readApiPayload } from "../lib/api";
import { type GitFile, type GitSnapshot } from "./git-workspace";

export interface GitReviewOptions {
  /** Workspace effectif de la conversation (peut être vide pour les canaux). */
  workspace: string | null | undefined;
  isAgentChannel: boolean;
  running: boolean;
  providerId: string;
  selectedModel: string;
}

/**
 * Regroupe l'état et les actions de la révision Git.
 *
 * Le hook suit le workspace de la conversation : il recharge le statut à chaque
 * changement de projet ou de run, et se vide pour les canaux d'agent qui n'ont
 * pas de dépôt associé.
 */
export function useGitReview({
  workspace,
  isAgentChannel,
  running,
  providerId,
  selectedModel,
}: GitReviewOptions) {
  const [gitSnapshot, setGitSnapshot] = useState<GitSnapshot | null>(null);
  const [gitBranches, setGitBranches] = useState<string[]>([]);
  const [gitSelectedFile, setGitSelectedFile] = useState<GitFile | null>(null);
  const [gitReviewSnapshot, setGitReviewSnapshot] = useState<GitSnapshot | null>(null);
  const [gitBusy, setGitBusy] = useState(false);
  const [gitError, setGitError] = useState("");
  const [gitCommitMessage, setGitCommitMessage] = useState<string | null>(null);
  const [gitCommitFingerprint, setGitCommitFingerprint] = useState("");
  const [gitCommitSource, setGitCommitSource] = useState("");

  const refreshGit = useCallback(async (target: string | null | undefined) => {
    // Sans workspace (ou canal d'agent sans dépôt), la revue se vide : passer
    // par ici plutôt que par l'effect évite un setState synchrone dans le corps
    // de l'effect.
    if (!target) {
      setGitSnapshot(null);
      setGitBranches([]);
      setGitReviewSnapshot(null);
      setGitSelectedFile(null);
      return;
    }
    try {
      const statusResponse = await fetch(
        `/api/kernel/git/status?workspace=${encodeURIComponent(target)}`,
      );
      const status = await readApiPayload<GitSnapshot>(statusResponse);
      setGitSnapshot(status.available ? status : null);
      // Les branches ne sont demandées qu'une fois le dépôt confirmé : sur un
      // projet non versionné, `/branches` répond 422 à chaque rafraîchissement.
      // L'erreur était rattrapée, mais elle inondait la console et masquait les
      // vraies. Une requête en série coûte moins qu'une erreur permanente.
      if (!status.available) {
        setGitBranches([]);
        return;
      }
      const branchesResponse = await fetch(
        `/api/kernel/git/branches?workspace=${encodeURIComponent(target)}`,
      );
      if (branchesResponse.ok) {
        const branchData = await readApiPayload<{ current: string; branches: string[] }>(branchesResponse);
        setGitBranches(branchData.branches);
      } else {
        setGitBranches([]);
      }
    } catch {
      setGitSnapshot(null);
      setGitBranches([]);
      setGitReviewSnapshot(null);
      setGitSelectedFile(null);
    }
  }, []);

  useEffect(() => {
    // Synchronise la revue Git avec le workspace de la conversation : recharge
    // quand il change, se vide quand il disparaît (ou en canal d'agent). Le
    // setState fait partie du rafraîchissement, pas d'un recalcul en cascade.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refreshGit(isAgentChannel ? null : workspace);
  }, [workspace, isAgentChannel, running, refreshGit]);

  async function switchGitBranch(branch: string) {
    if (!workspace || branch === gitSnapshot?.branch) return;
    setGitBusy(true);
    setGitError("");
    try {
      const response = await fetch("/api/kernel/git/switch", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ workspace, branch }),
      });
      await readApiPayload(response);
      await refreshGit(workspace);
    } catch (error) {
      setGitError(error instanceof Error ? error.message : "Changement de branche impossible.");
    } finally {
      setGitBusy(false);
    }
  }

  async function proposeGitCommit() {
    if (!workspace) return;
    setGitBusy(true);
    setGitError("");
    try {
      const response = await fetch("/api/kernel/git/commit-proposal", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ workspace, provider_id: providerId, model: selectedModel }),
      });
      const data = await readApiPayload<{ message: string; fingerprint: string; source: string }>(response);
      setGitCommitMessage(data.message);
      setGitCommitFingerprint(data.fingerprint);
      setGitCommitSource(data.source);
    } catch (error) {
      setGitError(error instanceof Error ? error.message : "Proposition impossible.");
    } finally {
      setGitBusy(false);
    }
  }

  async function commitGitChanges() {
    if (!workspace || gitCommitMessage === null) return;
    setGitBusy(true);
    setGitError("");
    try {
      const response = await fetch("/api/kernel/git/commit", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ workspace, message: gitCommitMessage, fingerprint: gitCommitFingerprint }),
      });
      await readApiPayload(response);
      setGitCommitMessage(null);
      // Les diffs n'existent plus apres le commit : on vide la revue.
      setGitReviewSnapshot(null);
      setGitSelectedFile(null);
      await refreshGit(workspace);
    } catch (error) {
      setGitError(error instanceof Error ? error.message : "Commit impossible.");
    } finally {
      setGitBusy(false);
    }
  }

  return {
    gitSnapshot,
    gitBranches,
    gitSelectedFile,
    gitReviewSnapshot,
    gitBusy,
    gitError,
    gitCommitMessage,
    gitCommitFingerprint,
    gitCommitSource,
    setGitSelectedFile,
    setGitReviewSnapshot,
    setGitCommitMessage,
    setGitError,
    refreshGit,
    switchGitBranch,
    proposeGitCommit,
    commitGitChanges,
  };
}
