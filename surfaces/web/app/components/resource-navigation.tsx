"use client";

import { Icon } from "../theme/theme-context";
import type { IconName } from "../theme/icons";

export type ResourceSection = "projects" | "kanban" | "agents" | "skills" | "providers" | "settings" | "crons";

type ResourceNavigationProps = {
  projectName?: string;
  workspaceCount: number;
  agentId?: string;
  agentCount: number;
  activeSkillCount: number;
  availableSkillCount: number;
  providerCount: number;
  defaultProvider?: string;
  settingsCount: number;
  cronCount: number;
  activeCronCount: number;
  unreadCronCount: number;
  onOpen: (section: ResourceSection) => void;
  /** Revient aux canaux permanents de l'agent, hors de tout projet. */
  onHome: () => void;
  /** Vrai quand aucun projet n'est actif : on est déjà dans cette vue. */
  homeActive: boolean;
  /** Canaux ayant du nouveau alors qu'on travaille ailleurs. */
  homeUnread: number;
};

/**
 * Navigation de contexte. Ordre de lecture volontaire :
 *   1. le titre de la section — l'information la plus stable, donc le repère ;
 *   2. ce qui est actif dessous — l'information qui change ;
 *   3. le nombre d'occurrences en pastille — purement indicatif.
 */
export function ResourceNavigation({
  projectName,
  workspaceCount,
  agentId,
  agentCount,
  activeSkillCount,
  availableSkillCount,
  providerCount,
  defaultProvider,
  settingsCount,
  cronCount,
  activeCronCount,
  unreadCronCount,
  onOpen,
  onHome,
  homeActive,
  homeUnread,
}: ResourceNavigationProps) {
  const cards: Array<{
    section: ResourceSection | "home";
    icon: IconName;
    title: string;
    active: string;
    count: number;
    /** Signale une nouveauté à traiter : la pastille passe en accent. */
    alert?: boolean;
    accentIcon?: boolean;
    action?: () => void;
    current?: boolean;
  }> = [
    {
      section: "projects",
      icon: "project",
      title: "Projets",
      active: projectName || "Aucun projet actif",
      count: workspaceCount,
    },
    {
      section: "agents",
      icon: "agent",
      title: "Agents",
      active: agentId || "Aucun agent actif",
      count: agentCount,
      accentIcon: true,
    },
    {
      section: "skills",
      icon: "skill",
      title: "Skills",
      active: activeSkillCount
        ? `${activeSkillCount} active${activeSkillCount > 1 ? "s" : ""}`
        : "Aucune skill active",
      count: availableSkillCount,
    },
    {
      section: "providers",
      icon: "provider",
      title: "Providers",
      active: defaultProvider || "Aucun provider par défaut",
      count: providerCount,
    },
    {
      section: "settings",
      icon: "settings",
      title: "Paramètres",
      active: settingsCount
        ? `${settingsCount} intégration${settingsCount > 1 ? "s" : ""}`
        : "Aucun paramètre requis",
      count: settingsCount,
    },
    {
      section: "crons",
      icon: "automation",
      title: "Routines",
      active: unreadCronCount
        ? `${unreadCronCount} nouveau${unreadCronCount > 1 ? "x" : ""} résultat${unreadCronCount > 1 ? "s" : ""}`
        : activeCronCount
          ? `${activeCronCount} active${activeCronCount > 1 ? "s" : ""}`
          : "Aucune routine active",
      count: cronCount,
      alert: unreadCronCount > 0,
    },
  ];

  return (
    <>
      {/* Deux destinations, une seule à la fois. Un sélecteur segmenté le dit
          d'un coup d'œil là où deux entrées de liste laissaient croire qu'on
          pouvait être dans les deux — on ouvrait alors un canal permanent en
          croyant rester dans le projet, et le travail partait dans l'espace
          personnel de l'agent sans que rien ne le signale. */}
      <div className="destination-switch" role="group" aria-label="Destination">
        <button
          type="button"
          className={homeActive ? "active" : ""}
          aria-pressed={homeActive}
          onClick={onHome}
        >
          <Icon name="agent" size="sm" />
          <span>Accueil</span>
          {homeUnread > 0 && !homeActive && (
            <em aria-label={`${homeUnread} canal avec du nouveau`}>{homeUnread}</em>
          )}
        </button>
        <button
          type="button"
          className={homeActive ? "" : "active"}
          aria-pressed={!homeActive}
          onClick={() => onOpen("projects")}
        >
          <Icon name="project" size="sm" />
          <span>{homeActive ? "Projets" : projectName || "Projets"}</span>
        </button>
      </div>
      <nav className="context-nav" aria-label="Contexte du run">
      {cards.map((card) => (
        <button
          className={`context-card${card.current ? " current" : ""}`}
          key={card.section}
          type="button"
          aria-current={card.current ? "true" : undefined}
          onClick={() => (card.action ? card.action() : onOpen(card.section as ResourceSection))}
        >
          <span className={`context-icon${card.accentIcon ? " agent-icon" : ""}`} aria-hidden="true">
            <Icon name={card.icon} size="sm" />
          </span>
          <span className="context-copy">
            <strong>{card.title}</strong>
            <em>{card.active}</em>
          </span>
          {card.count > 0 && (
            <span
              className={`context-count${card.alert ? " alert" : ""}`}
              aria-label={`${card.count} au total`}
            >
              {card.count > 99 ? "99+" : card.count}
            </span>
          )}
        </button>
        ))}
      </nav>
    </>
  );
}
