import { useCallback, useEffect, useRef, useState } from "react";
import { readApiPayload } from "../lib/api";
import { countLabel } from "../lib/format";
import {
  cronEditorFromJob,
  cronRequestBody,
  groupRoutineApprovals,
  scheduleFromEditor,
  workflowProposalError,
  workflowProposalFromPayload,
  workflowStateFromPayload,
  WorkflowProposalDisplayError,
  type CronApprovalStatus,
  type CronEditor,
  type CronJob,
  type CronRun,
  type CronTestFeedback,
  type CronTestResult,
  type CronWorkflowAction,
} from "../lib/cron";
import { type Approval } from "./approval-panel";
import { type ReasoningLevel, type SecurityMode } from "./composer-controls";
import { type ResourceSection } from "./resource-navigation";
import { type RoutineWorkflowProposal } from "./routine-workflow-panel";

export interface RoutinesOptions {
  managementModal: ResourceSection | null;
  agentId: string;
  selectedSkills: string[];
  securityMode: SecurityMode;
  providerId: string;
  selectedModel: string;
  reasoning: ReasoningLevel;
  /** Session du canal d'agent proposée par défaut aux notifications de routine. */
  notificationSessionId: string;
  /** Le skill workflow-creator figure au catalogue. */
  workflowCreatorAvailable: boolean;
  savingResource: boolean;
  activeSessionIdRef: { current: string | null };
  setManagementError: (message: string) => void;
  setUnreadSessionIds: (update: (current: Set<string>) => Set<string>) => void;
  setSavingResource: (value: boolean) => void;
}

/**
 * Regroupe l'état et les actions des routines (cronjobs) : édition, tests avec
 * préautorisations et workflow guidé.
 *
 * La liste est rafraîchie toutes les deux secondes, modale ouverte ou non :
 * les exécutions non lues alimentent les badges des sessions.
 */
export function useRoutines({
  managementModal,
  agentId,
  selectedSkills,
  securityMode,
  providerId,
  selectedModel,
  reasoning,
  notificationSessionId,
  workflowCreatorAvailable,
  savingResource,
  activeSessionIdRef,
  setManagementError,
  setUnreadSessionIds,
  setSavingResource,
}: RoutinesOptions) {
  const [cronJobs, setCronJobs] = useState<CronJob[]>([]);
  const [cronRuns, setCronRuns] = useState<CronRun[]>([]);
  const [cronEditor, setCronEditor] = useState<CronEditor | null>(null);
  const [cronTestApprovals, setCronTestApprovals] = useState<Approval[]>([]);
  const [cronTestMessage, setCronTestMessage] = useState("");
  const [cronTestFeedback, setCronTestFeedback] = useState<CronTestFeedback>("idle");
  const [cronTestDecision, setCronTestDecision] = useState<"approve" | "reject" | null>(null);
  const [cronApprovalStatus, setCronApprovalStatus] = useState<CronApprovalStatus | null>(null);
  const [testingCron, setTestingCron] = useState(false);
  const [cronWorkflowProposal, setCronWorkflowProposal] = useState<RoutineWorkflowProposal | null>(null);
  const [cronWorkflowAction, setCronWorkflowAction] = useState<CronWorkflowAction>(null);
  const [cronWorkflowFeedback, setCronWorkflowFeedback] = useState<"idle" | "success" | "error">("idle");
  const [cronWorkflowMessage, setCronWorkflowMessage] = useState("");
  const cronWorkflowProposalRequest = useRef(0);
  const cronWorkflowMutationRequest = useRef(0);

  const refreshCrons = useCallback(async () => {
    const [jobsResponse, runsResponse] = await Promise.all([
      fetch("/api/kernel/crons"),
      fetch("/api/kernel/crons/runs?limit=100"),
    ]);
    if (!jobsResponse.ok) throw new Error("Impossible de charger les cronjobs");
    setCronJobs(await jobsResponse.json());
    if (runsResponse.ok) {
      const payload: unknown = await runsResponse.json();
      const runs: CronRun[] = Array.isArray(payload) ? payload : [];
      setCronRuns(runs);
      const unreadSessions = runs
        .filter((item) =>
          item.delivery_status === "unread"
          && ["success", "failed", "partial", "timeout", "blocked"].includes(item.execution_status)
        )
        .map((item) => item.notification_session_id);
      if (unreadSessions.length) {
        setUnreadSessionIds((current) => {
          const updated = new Set(current);
          unreadSessions.forEach((id) => {
            if (id !== activeSessionIdRef.current) updated.add(id);
          });
          return updated;
        });
      }
    }
  }, [setUnreadSessionIds, activeSessionIdRef]);

  useEffect(() => {
    setManagementError("");
    // Le lancement du rafraîchissement fait partie de la synchronisation de la
    // liste des routines : les setState qu'il déclenche servent ce chargement,
    // pas un recalcul en cascade.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refreshCrons().catch((error) => {
      if (managementModal === "crons") {
        setManagementError(error instanceof Error ? error.message : "Erreur");
      }
    });
    const timer = window.setInterval(() => void refreshCrons().catch(() => undefined), 2000);
    return () => window.clearInterval(timer);
  }, [managementModal, refreshCrons, setManagementError]);

  function resetCronWorkflowState(message = "") {
    cronWorkflowProposalRequest.current += 1;
    cronWorkflowMutationRequest.current += 1;
    setCronWorkflowProposal(null);
    setCronWorkflowAction(null);
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage(message);
  }

  function invalidateCronWorkflowProposal() {
    const hadProposal = Boolean(cronWorkflowProposal || cronWorkflowAction === "propose");
    cronWorkflowProposalRequest.current += 1;
    if (!hadProposal) return;
    setCronWorkflowProposal(null);
    setCronWorkflowAction((current) => current === "propose" ? null : current);
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage(
      cronWorkflowAction === "propose"
        ? "La génération a été annulée car la base de la routine a changé. Relance-la quand tes modifications sont prêtes."
        : "La proposition a été retirée car la base de la routine a changé. Génère-la à nouveau.",
    );
  }

  function closeCronEditor() {
    resetCronWorkflowState();
    setCronEditor(null);
  }

  function createCron() {
    setManagementError("");
    setCronEditor(cronEditorFromJob({
      id: "", name: "Nouvelle routine", schedule: "0 9 * * *", prompt: "",
      workspace: null, agent_id: agentId, skills: selectedSkills,
      security_mode: securityMode, provider_id: providerId || null,
      model: selectedModel || null, reasoning, enabled: false, auto_resume: true,
      session_id: "", notification_session_id: notificationSessionId,
      next_run_at: null, last_run_at: null, last_status: null,
      last_error: null, in_flight: false, blocked: false,
      last_retryable: false,
    }, true));
    setCronTestApprovals([]);
    setCronTestMessage("");
    setCronTestFeedback("idle");
    setCronTestDecision(null);
    setCronApprovalStatus(null);
    resetCronWorkflowState();
  }

  async function openCronEditor(job: CronJob) {
    setManagementError("");
    setCronEditor(cronEditorFromJob(job));
    setCronTestApprovals([]);
    setCronTestMessage("Vérification des préautorisations…");
    setCronTestFeedback("progress");
    setCronTestDecision(null);
    setCronApprovalStatus(null);
    resetCronWorkflowState();
    try {
      const response = await fetch(`/api/kernel/crons/${job.id}/approval-status`);
      const status = await readApiPayload<CronApprovalStatus>(response);
      setCronApprovalStatus(status);
      const pending = await refreshCronTestApprovals(job.session_id, status.pending_run_id);
      if (pending.length > 0) {
        const scopeCount = groupRoutineApprovals(pending).length;
        setCronTestMessage(
          `${countLabel(scopeCount, "autorisation")} pour ${countLabel(pending.length, "action")} ${
            scopeCount === 1 ? "attend" : "attendent"
          } ta décision.`,
        );
        setCronTestFeedback("idle");
      } else if (status.approved_scopes.length > 0) {
        setCronTestMessage(
          `Prévalidation active · ${countLabel(status.approved_scopes.length, "périmètre")} ${
            status.approved_scopes.length === 1 ? "autorisé" : "autorisés"
          }.`,
        );
        setCronTestFeedback("success");
      } else {
        setCronTestMessage("Lance un test pour détecter les autorisations nécessaires.");
        setCronTestFeedback("idle");
      }
    } catch (error) {
      setCronTestMessage(
        error instanceof Error ? error.message : "Prévalidations impossibles à charger.",
      );
      setCronTestFeedback("error");
    }
  }

  async function loadCronApprovalStatus(jobId: string) {
    const response = await fetch(`/api/kernel/crons/${jobId}/approval-status`);
    const status = await readApiPayload<CronApprovalStatus>(response);
    setCronApprovalStatus(status);
    return status;
  }

  async function proposeCronWorkflow() {
    if (!cronEditor || cronWorkflowAction) return;
    const requestId = cronWorkflowProposalRequest.current + 1;
    cronWorkflowProposalRequest.current = requestId;
    setCronWorkflowAction("propose");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage("Analyse du prompt, des skills et des outils disponibles…");
    try {
      const response = await fetch("/api/kernel/crons/workflow-proposals", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(cronRequestBody(cronEditor)),
      });
      if (!response.ok) throw await workflowProposalError(response);
      const proposal = workflowProposalFromPayload(await readApiPayload<unknown>(response));
      if (cronWorkflowProposalRequest.current !== requestId) return;
      setCronWorkflowProposal(proposal);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage(
        proposal.warnings.length > 0
          ? `Proposition prête · ${countLabel(proposal.warnings.length, "point")} à vérifier.`
          : "Proposition prête. Elle ne sera enregistrée qu’après ta validation.",
      );
    } catch (error) {
      if (cronWorkflowProposalRequest.current !== requestId) return;
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(
        error instanceof WorkflowProposalDisplayError
          ? error.message
          : "Impossible de préparer le workflow guidé. Vérifie la connexion de l’application, puis réessaie.",
      );
    } finally {
      if (cronWorkflowProposalRequest.current === requestId) {
        setCronWorkflowAction(null);
      }
    }
  }

  async function acceptCronWorkflow() {
    if (!cronEditor || cronEditor.creating || !cronWorkflowProposal || cronWorkflowAction) return;
    const jobId = cronEditor.id;
    const proposal = cronWorkflowProposal;
    const requestId = cronWorkflowMutationRequest.current + 1;
    cronWorkflowMutationRequest.current = requestId;
    setCronWorkflowAction("accept");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage("Enregistrement du workflow…");
    try {
      const response = await fetch(`/api/kernel/crons/${jobId}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(cronRequestBody(cronEditor, proposal)),
      });
      const updated = await readApiPayload<CronJob>(response);
      if (cronWorkflowMutationRequest.current !== requestId) return;
      setCronEditor((current) => current?.id === jobId ? cronEditorFromJob(updated) : current);
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage(
        "Workflow et configuration enregistrés ensemble. Le prompt et les skills sont conservés.",
      );
      setCronApprovalStatus(null);
      setCronTestApprovals([]);
      setCronTestFeedback("idle");
      setCronTestMessage("Le workflow a changé. Relance un test pour prévalider ses actions.");
      await refreshCrons();
    } catch (error) {
      if (cronWorkflowMutationRequest.current !== requestId) return;
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(error instanceof Error ? error.message : "Enregistrement du workflow impossible.");
    } finally {
      if (cronWorkflowMutationRequest.current === requestId) {
        setCronWorkflowAction(null);
      }
    }
  }

  async function deleteCronWorkflow() {
    if (!cronEditor || cronEditor.creating || !cronEditor.workflow || cronWorkflowAction) return;
    if (!window.confirm(
      "Supprimer ce workflow ? La routine conservera son prompt et ses skills, mais l’agent retrouvera davantage de liberté. Les prévalidations devront être vérifiées à nouveau.",
    )) return;
    const jobId = cronEditor.id;
    const requestId = cronWorkflowMutationRequest.current + 1;
    cronWorkflowMutationRequest.current = requestId;
    setCronWorkflowAction("delete");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage("Suppression du workflow…");
    try {
      const response = await fetch(`/api/kernel/crons/${jobId}/workflow`, { method: "DELETE" });
      const payload = await readApiPayload<unknown>(response);
      if (cronWorkflowMutationRequest.current !== requestId) return;
      const state = workflowStateFromPayload(payload);
      setCronEditor((current) => current?.id === jobId ? ({
        ...current,
        workflow: null,
        workflow_revision: state.revision ?? current.workflow_revision,
        workflow_basis_hash: null,
        workflow_updated_at: null,
      }) : current);
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage("Workflow supprimé · la routine continuera avec son prompt et ses skills.");
      setCronApprovalStatus(null);
      setCronTestApprovals([]);
      setCronTestFeedback("idle");
      setCronTestMessage("Workflow supprimé. Relance un test pour prévalider le mode libre.");
      await refreshCrons();
    } catch (error) {
      if (cronWorkflowMutationRequest.current !== requestId) return;
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(error instanceof Error ? error.message : "Suppression du workflow impossible.");
    } finally {
      if (cronWorkflowMutationRequest.current === requestId) {
        setCronWorkflowAction(null);
      }
    }
  }

  async function continueWithoutCronWorkflow() {
    if (!cronEditor || cronWorkflowAction || savingResource || testingCron) return;
    if (cronEditor.workflow && persistedCron && cronWorkflowProposal) {
      setCronEditor(cronEditorFromJob(persistedCron));
      setCronWorkflowProposal(null);
      setCronWorkflowFeedback("success");
      setCronWorkflowMessage("Le workflow actuel est conservé sans modification.");
      return;
    }
    setCronWorkflowAction("continue");
    setCronWorkflowFeedback("idle");
    setCronWorkflowMessage(
      cronEditor.creating
        ? "Création de la routine en mode libre…"
        : "Enregistrement de la routine en mode libre…",
    );
    try {
      await saveCron(undefined, !cronEditor.creating);
    } finally {
      setCronWorkflowAction(null);
    }
  }

  async function acceptCronWorkflowChoice() {
    if (!cronEditor || !cronWorkflowProposal || cronWorkflowAction) return;
    if (cronEditor.creating) {
      setCronWorkflowAction("accept");
      setCronWorkflowFeedback("idle");
      setCronWorkflowMessage("Création de la routine avec ce workflow…");
      try {
        await saveCron(cronWorkflowProposal);
      } finally {
        setCronWorkflowAction(null);
      }
      return;
    }
    await acceptCronWorkflow();
  }

  async function saveCron(
    acceptedWorkflow?: RoutineWorkflowProposal,
    keepEditorOpen = false,
  ) {
    if (!cronEditor) return;
    const keptFreeInsteadOfProposal = keepEditorOpen && Boolean(cronWorkflowProposal);
    setSavingResource(true);
    setManagementError("");
    const body = cronRequestBody(cronEditor, acceptedWorkflow);
    try {
      const response = await fetch(
        `/api/kernel/crons${cronEditor.creating ? "" : `/${cronEditor.id}`}`,
        {
          method: cronEditor.creating ? "POST" : "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      const data = await readApiPayload<CronJob>(response);
      await refreshCrons();
      if (cronEditor.creating) {
        setCronEditor(cronEditorFromJob(data as CronJob));
        setCronWorkflowProposal(null);
        if (acceptedWorkflow) {
          setCronWorkflowFeedback("success");
          setCronWorkflowMessage("Routine et workflow enregistrés. Le prompt et les skills sont conservés.");
        } else {
          setCronWorkflowFeedback("success");
          setCronWorkflowMessage("Routine enregistrée sans workflow · exécution par prompt et skills.");
        }
        setCronTestMessage("Routine enregistrée et inactive. Teste-la avant de l’activer.");
      } else if (keepEditorOpen) {
        setCronEditor(cronEditorFromJob(data as CronJob));
        setCronWorkflowProposal(null);
        setCronWorkflowFeedback("success");
        setCronWorkflowMessage(
          keptFreeInsteadOfProposal
            ? "Mode libre conservé. La proposition n’a pas été appliquée."
            : "Routine enregistrée en mode libre.",
        );
        setCronTestMessage("Mode libre enregistré. Tu peux maintenant tester cette version.");
      } else {
        closeCronEditor();
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Enregistrement impossible";
      setManagementError(message);
      setCronWorkflowFeedback("error");
      setCronWorkflowMessage(message);
    } finally {
      setSavingResource(false);
    }
  }

  async function deleteCron(id: string) {
    if (!window.confirm("Supprimer ce cronjob ? Son historique de session sera conservé.")) return;
    try {
      const response = await fetch(`/api/kernel/crons/${id}`, { method: "DELETE" });
      await readApiPayload(response);
      await refreshCrons();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Suppression impossible");
    }
  }

  async function runCronNow(id: string) {
    try {
      const response = await fetch(`/api/kernel/crons/${id}/run`, { method: "POST" });
      const data = await readApiPayload<{ notification_session_id?: string }>(response);
      await refreshCrons();
      const notifiedSessionId = data.notification_session_id;
      if (notifiedSessionId) {
        setUnreadSessionIds((current) => new Set(current).add(notifiedSessionId));
      }
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Lancement impossible");
    }
  }

  async function toggleCron(job: CronJob, enabled: boolean) {
    const body = {
      name: job.name, schedule: job.schedule, prompt: job.prompt,
      workspace: job.workspace, agent_id: job.agent_id, skills: job.skills,
      security_mode: job.security_mode, provider_id: job.provider_id,
      model: job.model, reasoning: job.reasoning, enabled,
      auto_resume: job.auto_resume,
      notification_session_id: job.notification_session_id,
    };
    try {
      const response = await fetch(`/api/kernel/crons/${job.id}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      await readApiPayload<CronJob>(response);
      await refreshCrons();
    } catch (error) {
      setManagementError(error instanceof Error ? error.message : "Modification impossible");
    }
  }

  async function refreshCronTestApprovals(sessionId: string, runId?: string | null) {
    const response = await fetch("/api/kernel/approvals");
    const payload = await readApiPayload<Approval[]>(response);
    const pending: Approval[] = Array.isArray(payload) ? payload : [];
    const sessionApprovals = pending.filter((item) => item.session_id === sessionId);
    const selectedRunId = runId || sessionApprovals
      .slice()
      .sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)))[0]
      ?.run_id;
    const routineApprovals = selectedRunId
      ? sessionApprovals.filter((item) => item.run_id === selectedRunId)
      : [];
    setCronTestApprovals(routineApprovals);
    return routineApprovals;
  }

  async function testCron(id: string) {
    setTestingCron(true);
    setCronTestMessage("Test en cours…");
    setCronTestFeedback("progress");
    setCronTestDecision(null);
    setCronTestApprovals([]);
    try {
      const response = await fetch(`/api/kernel/crons/${id}/test`, { method: "POST" });
      const data = await readApiPayload<CronTestResult>(response);
      if (data.status === "approval_pending") {
        const pending = await refreshCronTestApprovals(data.session_id, data.run_id);
        const scopeCount = groupRoutineApprovals(pending).length;
        setCronTestMessage(
          `${countLabel(scopeCount, "autorisation")} ${scopeCount === 1 ? "couvre" : "couvrent"} ${
            countLabel(pending.length, "action")
          } ${pending.length === 1 ? "prévue" : "prévues"}.`,
        );
        setCronTestFeedback("idle");
      } else if (data.status === "success") {
        setCronTestMessage("Test réussi. La routine est prête.");
        setCronTestFeedback("success");
      } else {
        setCronTestMessage(data.errors?.map((error: {message: string}) => error.message).join("\n")
          || `Test terminé avec le statut ${data.status}.`);
        setCronTestFeedback("error");
      }
      await refreshCrons();
    } catch (error) {
      setCronTestMessage(error instanceof Error ? error.message : "Test impossible");
      setCronTestFeedback("error");
    } finally {
      setTestingCron(false);
    }
  }

  async function resolveCronTestApprovals(approved: boolean) {
    if (!cronEditor || cronTestApprovals.length === 0) return;
    const current = cronTestApprovals;
    const scopeCount = groupRoutineApprovals(current).length;
    setTestingCron(true);
    setCronTestDecision(approved ? "approve" : "reject");
    setCronTestFeedback("progress");
    setCronTestMessage(approved
      ? `Validation de ${countLabel(scopeCount, "autorisation")} couvrant ${
        countLabel(current.length, "action")
      } · reprise du test…`
      : `Enregistrement de ${countLabel(current.length, "refus", "refus")} · reprise du test…`);
    try {
      const response = await fetch("/api/kernel/approvals/resolve-batch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          approval_ids: current.map((approval) => approval.approval_id),
          approved,
        }),
      });
      const data = await readApiPayload<CronTestResult>(response);
      if (data.status === "approval_pending") {
        const pending = await refreshCronTestApprovals(cronEditor.session_id, data.run_id);
        const scopeCount = groupRoutineApprovals(pending).length;
        setCronTestMessage(
          `${scopeCount} ${scopeCount === 1 ? "nouvelle autorisation" : "nouvelles autorisations"} pour ${
            countLabel(pending.length, "action")
          } à examiner.`,
        );
        setCronTestFeedback("idle");
      } else if (data.status === "success") {
        setCronTestApprovals([]);
        setCronTestMessage(
          approved
            ? `Confirmé · ${countLabel(scopeCount, "autorisation")} ${
              scopeCount === 1 ? "enregistrée" : "enregistrées"
            } pour ${countLabel(current.length, "action")}. Test réussi.`
            : `Confirmé · ${countLabel(current.length, "refus", "refus")} ${
              current.length === 1 ? "enregistré" : "enregistrés"
            }. Test terminé.`,
        );
        setCronTestFeedback("success");
      } else {
        setCronTestApprovals([]);
        setCronTestMessage(
          `${approved ? "Autorisations enregistrées" : "Refus enregistrés"}, mais la reprise du test a échoué : ${
            data.errors?.map((error: {message: string}) => error.message).join("\n")
              || data.output || data.status
          }`,
        );
        setCronTestFeedback("error");
      }
      await loadCronApprovalStatus(cronEditor.id);
      await refreshCrons();
    } catch (error) {
      try {
        const pending = await refreshCronTestApprovals(cronEditor.session_id);
        if (pending.length === 0) {
          setCronTestMessage(
            "La décision a été reçue, mais la reprise du test n’a pas pu être confirmée.",
          );
        } else {
          setCronTestMessage(error instanceof Error ? error.message : "Résolution impossible");
        }
      } catch {
        setCronTestApprovals(current);
        setCronTestMessage("Impossible de vérifier si la décision a été enregistrée. Réessaie dans un instant.");
      }
      setCronTestFeedback("error");
    } finally {
      setTestingCron(false);
      setCronTestDecision(null);
    }
  }

  const cronApprovalGroups = groupRoutineApprovals(cronTestApprovals);
  const persistedCron = cronEditor && !cronEditor.creating
    ? cronJobs.find((job) => job.id === cronEditor.id)
    : null;
  const cronPermissionConfigDirty = Boolean(cronEditor && persistedCron && (
    cronEditor.prompt !== persistedCron.prompt
    || cronEditor.workspace !== persistedCron.workspace
    || cronEditor.agent_id !== persistedCron.agent_id
    || JSON.stringify(cronEditor.skills) !== JSON.stringify(persistedCron.skills)
    || cronEditor.security_mode !== persistedCron.security_mode
    || cronEditor.provider_id !== persistedCron.provider_id
    || cronEditor.model !== persistedCron.model
    || cronEditor.reasoning !== persistedCron.reasoning
  ));
  const cronWorkflowBasisDirty = Boolean(cronEditor && persistedCron && (
    cronEditor.name.trim() !== persistedCron.name
    || scheduleFromEditor(cronEditor) !== persistedCron.schedule
    || cronEditor.prompt !== persistedCron.prompt
    || cronEditor.workspace !== persistedCron.workspace
    || cronEditor.agent_id !== persistedCron.agent_id
    || JSON.stringify([...cronEditor.skills].sort())
      !== JSON.stringify([...persistedCron.skills].sort())
    || cronEditor.security_mode !== persistedCron.security_mode
  ));
  const cronWorkflowProposalReady = cronWorkflowProposal?.workflow.status === "ready";
  const cronWorkflowMutationAvailable = Boolean(
    cronEditor
    && !cronEditor.creating
    && !cronEditor.in_flight
    && !cronEditor.blocked
    && !testingCron
    && !savingResource
    && !cronApprovalStatus?.pending_count,
  );
  const cronWorkflowMutationBusy = cronWorkflowAction === "accept"
    || cronWorkflowAction === "delete";
  const cronWorkflowCanAccept = Boolean(
    cronEditor
    && cronWorkflowProposal
    && !cronEditor.in_flight
    && !cronEditor.blocked
    && !testingCron
    && !savingResource
    && !cronWorkflowMutationBusy
    && (!cronEditor.enabled || cronWorkflowProposalReady),
  );
  const cronWorkflowCanPropose = Boolean(
    cronEditor
    && workflowCreatorAvailable
    && cronEditor.name.trim()
    && cronEditor.prompt.trim()
    && !savingResource,
  );
  const cronCanSave = Boolean(
    cronEditor
    && cronEditor.name.trim()
    && cronEditor.prompt.trim()
    && !savingResource
    && !testingCron
    && !cronWorkflowAction
    && !cronWorkflowProposal
    && !(cronEditor.workflow && cronWorkflowBasisDirty)
    && !(cronEditor.workflow && cronEditor.enabled && cronEditor.workflow.status !== "ready"),
  );
  const cronWorkflowCanContinue = Boolean(
    cronEditor
    && cronEditor.name.trim()
    && cronEditor.prompt.trim()
    && !savingResource
    && !testingCron
    && !cronWorkflowAction,
  );

  const unreadCronCount = cronRuns.filter((item) => item.delivery_status === "unread").length;

  return {
    cronJobs,
    cronEditor,
    cronTestApprovals,
    cronTestMessage,
    cronTestFeedback,
    cronTestDecision,
    cronApprovalStatus,
    testingCron,
    cronWorkflowProposal,
    cronWorkflowAction,
    cronWorkflowFeedback,
    cronWorkflowMessage,
    setCronEditor,
    workflowCreatorAvailable,
    cronApprovalGroups,
    cronPermissionConfigDirty,
    cronWorkflowBasisDirty,
    cronWorkflowProposalReady,
    cronWorkflowMutationAvailable,
    cronWorkflowMutationBusy,
    cronWorkflowCanAccept,
    cronWorkflowCanPropose,
    cronCanSave,
    cronWorkflowCanContinue,
    unreadCronCount,
    invalidateCronWorkflowProposal,
    closeCronEditor,
    createCron,
    openCronEditor,
    proposeCronWorkflow,
    deleteCronWorkflow,
    continueWithoutCronWorkflow,
    acceptCronWorkflowChoice,
    saveCron,
    deleteCron,
    runCronNow,
    toggleCron,
    testCron,
    resolveCronTestApprovals,
  };
}
