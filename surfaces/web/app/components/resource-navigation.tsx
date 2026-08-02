"use client";

import {
  Bot,
  CalendarClock,
  ChevronRight,
  FolderOpen,
  PlugZap,
  Sparkles,
} from "lucide-react";

export type ResourceSection = "projects" | "agents" | "skills" | "providers" | "crons";

type ResourceNavigationProps = {
  projectName?: string;
  projectPath?: string;
  agentId?: string;
  agentProvider?: string;
  selectedSkillCount: number;
  availableSkillCount: number;
  providerCount: number;
  defaultProvider?: string;
  activeCronCount: number;
  unreadCronCount: number;
  onOpen: (section: ResourceSection) => void;
};

export function ResourceNavigation({
  projectName,
  projectPath,
  agentId,
  agentProvider,
  selectedSkillCount,
  availableSkillCount,
  providerCount,
  defaultProvider,
  activeCronCount,
  unreadCronCount,
  onOpen,
}: ResourceNavigationProps) {
  const cards = [
    {
      section: "projects" as const,
      label: "Projet actif",
      title: projectName || "Choisir un projet",
      detail: projectPath || "Aucun CWD",
      icon: <FolderOpen />,
    },
    {
      section: "agents" as const,
      label: "Agent actif",
      title: agentId || "Aucun agent",
      detail: agentProvider || "Non configuré",
      icon: <Bot />,
      iconClassName: "agent-icon",
    },
    {
      section: "skills" as const,
      label: "Skills",
      title: selectedSkillCount
        ? `${selectedSkillCount} sélectionnée${selectedSkillCount > 1 ? "s" : ""}`
        : "Aucune sélection",
      detail: `${availableSkillCount} disponible${availableSkillCount > 1 ? "s" : ""}`,
      icon: <Sparkles />,
    },
    {
      section: "providers" as const,
      label: "Providers",
      title: `${providerCount} configuré${providerCount > 1 ? "s" : ""}`,
      detail: `Défaut · ${defaultProvider || "aucun"}`,
      icon: <PlugZap />,
    },
    {
      section: "crons" as const,
      label: "Automatisations",
      title: `${activeCronCount} active${activeCronCount > 1 ? "s" : ""}`,
      detail: unreadCronCount
        ? `${unreadCronCount} nouveau${unreadCronCount > 1 ? "x" : ""} résultat${unreadCronCount > 1 ? "s" : ""}`
        : "Cronjobs et reprises",
      icon: <CalendarClock />,
    },
  ];

  return (
    <nav className="context-nav" aria-label="Contexte du run">
      {cards.map((card) => (
        <button
          className="context-card"
          key={card.section}
          onClick={() => onOpen(card.section)}
        >
          <span
            className={`context-icon ${card.iconClassName || ""}`.trim()}
            aria-hidden="true"
          >
            {card.icon}
          </span>
          <span>
            <small>{card.label}</small>
            <strong>{card.title}</strong>
            <em>{card.detail}</em>
          </span>
          <ChevronRight className="context-chevron" aria-hidden="true" />
        </button>
      ))}
    </nav>
  );
}
