"use client";

import { Icon } from "../theme/theme-context";
import type { IconName } from "../theme/icons";

export type ResourceSection = "projects" | "agents" | "skills" | "providers" | "settings" | "crons";

type ResourceNavigationProps = {
  projectName?: string;
  workspaceCount: number;
  agentId?: string;
  agentCount: number;
  selectedSkillCount: number;
  availableSkillCount: number;
  providerCount: number;
  defaultProvider?: string;
  settingsCount: number;
  cronCount: number;
  activeCronCount: number;
  unreadCronCount: number;
  onOpen: (section: ResourceSection) => void;
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
  selectedSkillCount,
  availableSkillCount,
  providerCount,
  defaultProvider,
  settingsCount,
  cronCount,
  activeCronCount,
  unreadCronCount,
  onOpen,
}: ResourceNavigationProps) {
  const cards: Array<{
    section: ResourceSection;
    icon: IconName;
    title: string;
    active: string;
    count: number;
    /** Signale une nouveauté à traiter : la pastille passe en accent. */
    alert?: boolean;
    accentIcon?: boolean;
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
      active: selectedSkillCount
        ? `${selectedSkillCount} sélectionnée${selectedSkillCount > 1 ? "s" : ""}`
        : "Aucune sélection",
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
    <nav className="context-nav" aria-label="Contexte du run">
      {cards.map((card) => (
        <button
          className="context-card"
          key={card.section}
          type="button"
          onClick={() => onOpen(card.section)}
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
  );
}
