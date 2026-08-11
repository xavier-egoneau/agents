"use client";

/**
 * ÉDITEUR DE ROUTINE
 * -----------------------------------------------------------------------------
 * Quatre cents lignes de formulaire, sorties de `page.tsx` sans changer une
 * ligne de leur logique. Tout l'état vient de `useRoutines`, qui le tenait
 * déjà : le composant reçoit l'objet du hook en bloc plutôt que trente
 * propriétés séparées, ce qui garde la vérification de types entière sans
 * transformer l'extraction en réécriture.
 */

import { RoutineWorkflowPanel } from "./routine-workflow-panel";
import type { useRoutines } from "./use-routines";
import { describeCron, type CronFrequencyKind, type CronJob } from "../lib/cron";
import type { Catalog, SessionSummary, Workspace } from "../lib/model";
import type { SecurityMode } from "./composer-controls";

type RoutineEditorProps = {
  routines: ReturnType<typeof useRoutines>;
  savingResource: boolean;
  sessions: SessionSummary[];
  catalog: Catalog | null;
  /** Mode du composer, affiché comme repère à côté de celui de la routine. */
  securityMode: SecurityMode;
  workspaces: Workspace[];
  onSave: () => void;
};

export function RoutineEditor({
  routines,
  savingResource,
  sessions,
  catalog,
  securityMode,
  workspaces,
  onSave,
}: RoutineEditorProps) {
  const {
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
    invalidateCronWorkflowProposal,
    closeCronEditor,
    proposeCronWorkflow,
    acceptCronWorkflowChoice,
    continueWithoutCronWorkflow,
    deleteCronWorkflow,
    testCron,
    resolveCronTestApprovals,
  } = routines;
  if (!cronEditor) return null;
  return (
    <div className="resource-editor cron-editor">
      <button
        type="button"
        className="editor-back"
        onClick={closeCronEditor}
        disabled={cronWorkflowMutationBusy || savingResource || testingCron}
      >← Retour aux routines</button>
      <fieldset
        className="resource-fields"
        disabled={cronWorkflowMutationBusy || savingResource || testingCron}
      >
        <label className="field-wide">
          Nom
          <input value={cronEditor.name} onChange={(event) => {
            invalidateCronWorkflowProposal();
            setCronEditor((current) => current && ({ ...current, name: event.target.value }));
          }}
          />
        </label>
        <label>
          Répétition
          <select value={cronEditor.frequency_kind} onChange={(event) => {
            invalidateCronWorkflowProposal();
            setCronEditor((current) => current && ({
              ...current, frequency_kind: event.target.value as CronFrequencyKind,
            }));
          }}>
            <option value="minutes">Toutes les X minutes</option>
            <option value="hours">Toutes les X heures</option>
            <option value="daily">Tous les jours</option>
            <option value="weekly">Toutes les semaines</option>
            <option value="yearly">Tous les ans</option>
            <option value="once">Ponctuel (date précise)</option>
          </select>
        </label>
        {cronEditor.frequency_kind === "once" && (
          <label>
            Date et heure
            <input type="datetime-local"
              value={cronEditor.one_shot_datetime}
              onChange={(event) => {
                invalidateCronWorkflowProposal();
                setCronEditor((current) => current && ({
                  ...current, one_shot_datetime: event.target.value,
                }));
              }}
            />
          </label>
        )}
        {(cronEditor.frequency_kind === "minutes" || cronEditor.frequency_kind === "hours") && (
          <label>
            Intervalle
            <input type="number" min="1"
              max={cronEditor.frequency_kind === "minutes" ? 59 : 23}
              value={cronEditor.frequency_interval}
              onChange={(event) => {
                invalidateCronWorkflowProposal();
                setCronEditor((current) => current && ({
                  ...current, frequency_interval: Math.max(1, Number(event.target.value)),
                }));
              }}
            />
          </label>
        )}
        {cronEditor.frequency_kind === "weekly" && (
          <label>
            Jour
            <select value={cronEditor.frequency_weekday} onChange={(event) => {
              invalidateCronWorkflowProposal();
              setCronEditor((current) => current && ({
                ...current, frequency_weekday: Number(event.target.value),
              }));
            }}>
              {["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"]
                .map((day, index) => <option value={index} key={day}>{day}</option>)}
            </select>
          </label>
        )}
        {cronEditor.frequency_kind === "yearly" && (
          <>
            <label>
              Mois
              <select value={cronEditor.frequency_month} onChange={(event) => {
                invalidateCronWorkflowProposal();
                setCronEditor((current) => current && ({
                  ...current, frequency_month: Number(event.target.value),
                }));
              }}>
                {["Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
                  "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]
                  .map((month, index) => <option value={index + 1} key={month}>{month}</option>)}
              </select>
            </label>
            <label>
              Jour du mois
              <input type="number" min="1" max="31" value={cronEditor.frequency_monthday}
                onChange={(event) => {
                  invalidateCronWorkflowProposal();
                  setCronEditor((current) => current && ({
                    ...current, frequency_monthday: Math.min(31, Math.max(1, Number(event.target.value))),
                  }));
                }}
              />
            </label>
          </>
        )}
        {["daily", "weekly", "yearly"].includes(cronEditor.frequency_kind) && (
          <label>
            Heure
            <input type="time" value={cronEditor.frequency_time} onChange={(event) => {
              invalidateCronWorkflowProposal();
              setCronEditor((current) => current && ({
                ...current, frequency_time: event.target.value,
              }));
            }}
            />
          </label>
        )}
        <div className="field-wide cron-summary">
          <span>Prochaine règle</span>
          <strong>{describeCron(cronEditor)}</strong>
        </div>
        <label>
          Agent
          <select value={cronEditor.agent_id} onChange={(event) => {
            invalidateCronWorkflowProposal();
            setCronEditor((current) => current && ({
              ...current, agent_id: event.target.value,
            }));
          }}>
            {catalog?.agents.map((agent) => (
              <option value={agent.id} key={agent.id}>{agent.id}</option>
            ))}
          </select>
        </label>
        <label>
          Permission de la routine
          <select value={cronEditor.security_mode} onChange={(event) => {
            invalidateCronWorkflowProposal();
            setCronEditor((current) => current && ({
              ...current,
              security_mode: event.target.value as CronJob["security_mode"],
            }));
          }}>
            <option value="safe">Prudent</option>
            <option value="limited">Limité</option>
            <option value="power">Étendu</option>
          </select>
          <small>
            {cronEditor.creating
              ? `Initialisée depuis la conversation active (${securityMode}). Tu peux la modifier ici.`
              : `Réglage enregistré pour cette routine. La conversation active est en ${securityMode}.`}
          </small>
        </label>
        <fieldset className="field-wide checkbox-field cron-skills-field">
          <legend>Skills de la routine</legend>
          <p>
            Ces skills restent actives avec ou sans workflow. Aucune sélection signifie que
            l’agent suit seulement le prompt.
          </p>
          <div className="checkbox-grid">
            {catalog?.skills
              .filter((skill) => skill.name !== "workflow-creator")
              .map((skill) => (
                <label key={skill.name} title={skill.description}>
                  <input
                    type="checkbox"
                    checked={cronEditor.skills.includes(skill.name)}
                    onChange={() => {
                      invalidateCronWorkflowProposal();
                      setCronEditor((current) => current && ({
                        ...current,
                        skills: current.skills.includes(skill.name)
                          ? current.skills.filter((item) => item !== skill.name)
                          : [...current.skills, skill.name],
                      }));
                    }}
                  />
                  <span><strong>{skill.name}</strong><small>{skill.description}</small></span>
                </label>
              ))}
            {catalog?.skills.filter((skill) => skill.name !== "workflow-creator").length === 0 && (
              <p>Aucune skill d’exécution installée.</p>
            )}
          </div>
        </fieldset>
        <label className="field-wide">
          Workspace
          <select value={cronEditor.workspace || ""} onChange={(event) => {
            invalidateCronWorkflowProposal();
            setCronEditor((current) => current && ({
              ...current, workspace: event.target.value || null,
            }));
          }}
          >
            <option value="">Espace personnel de l’agent (par défaut)</option>
            {workspaces.map((workspace) => (
              <option value={workspace.path} key={workspace.path}>{workspace.name}</option>
            ))}
            {/* Sans cette option, un chemin absent de la liste — base
                déplacée, projet supprimé — ne correspond à aucune
                entrée : le select affiche « Aucun » alors que la
                routine porte toujours ce dossier. */}
            {cronEditor.workspace
              && !workspaces.some((item) => item.path === cronEditor.workspace) && (
              <option value={cronEditor.workspace}>
                {cronEditor.workspace} — dossier introuvable
              </option>
            )}
          </select>
        </label>
        {cronEditor.workspace
          && !workspaces.some((item) => item.path === cronEditor.workspace) && (
          <p className="cron-note field-wide">
            Ce dossier n’existe pas sur cette machine. Choisis l’espace personnel de
            l’agent, ou sélectionne un projet existant.
          </p>
        )}
        <label className="field-wide">
          Résultats envoyés dans
          <select value={cronEditor.notification_session_id} onChange={(event) =>
            setCronEditor((current) => current && ({
              ...current, notification_session_id: event.target.value,
            }))}>
            {sessions.map((session) => (
              <option value={session.session_id} key={session.session_id}>
                {session.trigger === "agent_channel"
                  ? `${session.agent_id} · canonique (par défaut)`
                  : session.prompt || "Session sans titre"}
              </option>
            ))}
          </select>
        </label>
        <label className="field-wide">
          Demande exécutée
          <textarea value={cronEditor.prompt} onChange={(event) => {
            invalidateCronWorkflowProposal();
            setCronEditor((current) => current && ({ ...current, prompt: event.target.value }));
          }}
            placeholder="Décris le résultat attendu à chaque exécution…"
          />
        </label>
        <div className="field-wide switch-setting">
          <span><strong>Routine active</strong><small>Exécuter selon la fréquence choisie</small></span>
          <button type="button" role="switch" aria-checked={cronEditor.enabled}
            className={`toggle-switch ${cronEditor.enabled ? "on" : ""}`}
            onClick={() => setCronEditor((current) => current && ({
              ...current, enabled: !current.enabled,
            }))}><span /></button>
        </div>
        <div className="field-wide switch-setting">
          <span><strong>Reprise après échec</strong><small>Continuer au prochain passage après une erreur transitoire</small></span>
          <button type="button" role="switch" aria-checked={cronEditor.auto_resume}
            className={`toggle-switch ${cronEditor.auto_resume ? "on" : ""}`}
            onClick={() => setCronEditor((current) => current && ({
              ...current, auto_resume: !current.auto_resume,
            }))}><span /></button>
        </div>
      </fieldset>
      <RoutineWorkflowPanel
        workflow={cronEditor.workflow || null}
        proposal={cronWorkflowProposal}
        revision={cronEditor.workflow_revision}
        updatedAt={cronEditor.workflow_updated_at}
        action={cronWorkflowAction}
        feedback={
          cronEditor.workflow && cronWorkflowBasisDirty && !cronWorkflowProposal
            ? "warning"
            : cronWorkflowFeedback
        }
        message={
          cronWorkflowProposal && cronEditor.enabled && !cronWorkflowProposalReady
            ? "Ce workflow n’est pas prêt. Désactive la routine pour l’enregistrer comme brouillon, ou corrige ses dépendances."
            : cronWorkflowProposal && (cronEditor.blocked || cronEditor.in_flight)
              ? "La routine exécute une occurrence réelle ou attend son autorisation. Termine-la avant de changer son mode d’exécution."
              : cronWorkflowProposal && Boolean(cronApprovalStatus?.pending_count)
                ? "Accepter ce workflow remplacera le test du mode libre en attente. Ses autorisations seront annulées et le nouveau workflow devra être testé."
              : cronEditor.workflow && !cronWorkflowMutationAvailable
                ? "Le workflow reste consultable. Attends la fin de l’exécution ou de la validation pour le modifier ou le supprimer."
            : !cronEditor.creating
            && cronEditor.workflow
            && cronWorkflowBasisDirty
            && !cronWorkflowProposal
            ? "Ces modifications rendent le workflow actif obsolète. Propose une nouvelle version ou supprime le workflow avant d’enregistrer."
            : cronWorkflowMessage || (!workflowCreatorAvailable
              ? "Le générateur workflow-creator n’apparaît pas dans l’application active. Redémarre l’application après son installation ; le mode libre reste disponible."
              : "")
        }
        creating={cronEditor.creating}
        canPropose={cronWorkflowCanPropose}
        canContinueWithoutWorkflow={cronWorkflowCanContinue}
        canAcceptProposal={cronWorkflowCanAccept}
        canDelete={cronWorkflowMutationAvailable}
        onPropose={() => void proposeCronWorkflow()}
        onContinueWithoutWorkflow={() => void continueWithoutCronWorkflow()}
        onAcceptProposal={() => void acceptCronWorkflowChoice()}
        onDelete={() => void deleteCronWorkflow()}
      />
      {!cronWorkflowProposal && !cronEditor.creating && (() => {
        // Le bloc n'est une alerte que s'il y a réellement quelque chose
        // à trancher. Prévalidé et sans modification en attente, il se
        // réduit à une ligne de statut.
        const pending = cronTestApprovals.length > 0;
        const prevalidated = Boolean(
          cronApprovalStatus?.approved_scopes.length
          && !cronPermissionConfigDirty
          && !testingCron
          && !pending,
        );
        const tone = pending
          ? "attention"
          : cronTestFeedback === "idle"
            ? prevalidated ? "settled" : "neutral"
            : cronTestFeedback;

        return (
          <div className={`cron-test-panel ${tone}`} aria-busy={testingCron}>
            <div className="cron-test-heading">
              <strong>
                {pending
                  ? "Autorisations à accorder"
                  : prevalidated
                    ? "Routine prévalidée"
                    : "Prévalidation"}
              </strong>
              {prevalidated && (
                <span className="cron-prevalidation-badge">
                  {cronApprovalStatus?.approved_scopes.length} autorisation
                  {(cronApprovalStatus?.approved_scopes.length || 0) > 1 ? "s" : ""}
                </span>
              )}
              {(!prevalidated || cronTestMessage) && (
                <small
                  role={cronTestFeedback === "error" ? "alert" : "status"}
                  aria-live="polite"
                >
                  {testingCron && <span className="cron-test-spinner" aria-hidden="true" />}
                  {cronPermissionConfigDirty
                    ? "Enregistre les modifications avant de relancer la prévalidation."
                    : cronTestMessage
                      || "Un test détecte les autorisations dont la routine aura besoin."}
                </small>
              )}
            </div>

            {pending && (
              <ul>
                {cronApprovalGroups.map((group) => (
                  <li key={group.key}>
                    <strong>{group.tool_name}</strong>
                    <span>
                      {group.count > 1 ? `${group.count} actions prévues · ` : ""}
                      {group.justifications[0]}
                    </span>
                    {group.path && <code>{group.path}</code>}
                  </li>
                ))}
              </ul>
            )}

            <div className="cron-test-actions">
              {pending ? (
                <>
                  <button disabled={testingCron || cronPermissionConfigDirty}
                    onClick={() => void resolveCronTestApprovals(false)}>
                    {cronTestDecision === "reject" ? "Refus en cours…" : "Tout refuser"}
                  </button>
                  <button className="primary" disabled={testingCron || cronPermissionConfigDirty}
                    onClick={() => void resolveCronTestApprovals(true)}>
                    {cronTestDecision === "approve"
                      ? "Autorisation en cours…"
                      : "Autoriser cette routine"}
                  </button>
                </>
              ) : (
                <button
                  disabled={testingCron || cronEditor.in_flight || cronPermissionConfigDirty}
                  onClick={() => void testCron(cronEditor.id)}
                  title="Les demandes validées pendant le test sont mémorisées pour cette routine, uniquement pour l’outil et la cible affichés."
                >
                  {testingCron
                    ? "Test en cours…"
                    : cronPermissionConfigDirty
                      ? "Enregistrer avant de tester"
                      : prevalidated
                        ? "Retester"
                        : "Tester la routine"}
                </button>
              )}
            </div>
          </div>
        );
      })()}
      <div className="resource-editor-actions">
        <button
          onClick={closeCronEditor}
          disabled={cronWorkflowMutationBusy || savingResource || testingCron}
        >Annuler</button>
        {!cronWorkflowProposal && (
          <button className="primary" onClick={() => onSave()} disabled={!cronCanSave}>
            {savingResource
              ? "Enregistrement…"
              : cronEditor.workflow && cronWorkflowBasisDirty
                ? "Workflow à mettre à jour"
                : "Enregistrer"}
          </button>
        )}
      </div>
    </div>
  );
}
