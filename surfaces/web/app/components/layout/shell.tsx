"use client";

import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from "react";
import { Icon } from "../../theme/theme-context";
import type { IconName } from "../../theme/icons";
import type { PanelKey } from "./use-panels";

/* ---------------------------------------------------------------------------
   Primitives de layout. Elles ne connaissent rien du metier : on leur passe
   du contenu. Toute la peau vient des tokens.
--------------------------------------------------------------------------- */

export function Shell({
  leftOpen,
  rightOpen,
  leftWidth,
  rightWidth,
  resizing,
  children,
}: {
  leftOpen: boolean;
  rightOpen: boolean;
  leftWidth: number;
  rightWidth: number;
  resizing: PanelKey | null;
  children: ReactNode;
}) {
  return (
    <main
      className="shell"
      data-left={leftOpen ? "open" : "closed"}
      data-right={rightOpen ? "open" : "closed"}
      data-resizing={resizing ? "true" : "false"}
      style={
        {
          "--w-sidepanel": `${leftWidth}px`,
          "--w-dock": `${rightWidth}px`,
        } as CSSProperties
      }
    >
      {children}
    </main>
  );
}

export function Resizer({
  side,
  label,
  active,
  onPointerDown,
  onKeyDown,
}: {
  side: "start" | "end";
  label: string;
  active: boolean;
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
}) {
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      tabIndex={0}
      className={`resizer at-${side}`}
      data-active={active ? "true" : "false"}
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
    />
  );
}

/* --- Rail d'icones -------------------------------------------------------- */

export type RailEntry = {
  id: string;
  icon: IconName;
  label: string;
  badge?: number | "dot";
  onSelect: () => void;
};

export function IconRail({
  brand,
  onBrandClick,
  entries,
  activeId,
  collapsed,
  footer,
}: {
  brand: string;
  onBrandClick: () => void;
  entries: RailEntry[];
  activeId?: string;
  /**
   * Le rail disparaît quand le panneau est ouvert : il en est le substitut, pas
   * un doublon. En dessous de 900 px le panneau devient un tiroir superposé et
   * le rail reste visible — d'où la règle `visibility` en media query.
   */
  collapsed?: boolean;
  footer: ReactNode;
}) {
  return (
    <nav className="icon-rail" aria-label="Navigation principale" inert={collapsed || undefined}>
      <button className="rail-brand" onClick={onBrandClick} data-tip="Nouvelle conversation" type="button">
        {brand}
      </button>

      <div className="rail-nav">
        {entries.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className="rail-item"
            data-tip={entry.label}
            aria-label={entry.label}
            aria-current={activeId === entry.id ? "true" : undefined}
            onClick={entry.onSelect}
          >
            <Icon name={entry.icon} size="md" />
            {entry.badge === "dot" && <span className="rail-badge dot" />}
            {typeof entry.badge === "number" && entry.badge > 0 && (
              <span className="rail-badge">{entry.badge > 99 ? "99+" : entry.badge}</span>
            )}
          </button>
        ))}
      </div>

      <div className="rail-foot">{footer}</div>
    </nav>
  );
}

/* --- Panneau lateral gauche ----------------------------------------------- */

export function SidePanel({
  title,
  collapsed,
  actions,
  footer,
  children,
  resizer,
}: {
  title: string;
  collapsed: boolean;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  resizer: ReactNode;
}) {
  return (
    <div className="panel-shell">
      {/* `inert` retire le panneau replie du focus et des lecteurs d'ecran
          sans le demonter : la transition de largeur reste fluide. */}
      <aside className="side-panel" aria-label={title} inert={collapsed || undefined}>
        <header className="side-panel-head">
          <h2>{title}</h2>
          <div style={{ display: "flex", gap: 2 }}>{actions}</div>
        </header>
        <div className="side-panel-body">{children}</div>
        {footer && <footer className="side-panel-foot">{footer}</footer>}
      </aside>
      {!collapsed && resizer}
    </div>
  );
}

/**
 * Section du panneau gauche. `grow` designe la section qui absorbe la hauteur
 * restante et scrolle seule — les sections au-dessus restent donc toujours
 * visibles, quelle que soit la longueur de la liste.
 */
export function SidePanelSection({
  title,
  count,
  action,
  grow,
  children,
}: {
  title?: string;
  count?: number;
  action?: ReactNode;
  grow?: boolean;
  children: ReactNode;
}) {
  return (
    <section className={grow ? "side-panel-section grow" : "side-panel-section"}>
      {(title || action) && (
        <header className="side-panel-section-head">
          {title && <p className="eyebrow">{title}</p>}
          {typeof count === "number" && <span>{count}</span>}
          {action}
        </header>
      )}
      <div className="side-panel-section-body">{children}</div>
    </section>
  );
}

/* --- Dock droit ------------------------------------------------------------ */

export type DockTab = {
  id: string;
  icon: IconName;
  label: string;
  badge?: number | "dot";
};

export function Dock({
  tabs,
  activeTab,
  onTabChange,
  collapsed,
  title,
  subtitle,
  actions,
  children,
  resizer,
}: {
  tabs: DockTab[];
  activeTab: string;
  onTabChange: (id: string) => void;
  collapsed: boolean;
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  resizer: ReactNode;
}) {
  return (
    <div className="panel-shell">
      <aside className="dock" aria-label="Panneau lateral droit" inert={collapsed || undefined}>
        <div className="dock-body">
          <header className="dock-head">
            <div className="dock-head-title">
              <strong>{title}</strong>
              {subtitle && <small>{subtitle}</small>}
            </div>
            <div style={{ display: "flex", gap: 2 }}>{actions}</div>
          </header>
          {/* Le contenu n'est monte qu'a l'ouverture : evite les requetes
              inutiles de l'explorateur quand le dock est replie. */}
          <div className="dock-content">{collapsed ? null : children}</div>
        </div>

        <div className="dock-tabs" role="tablist" aria-orientation="vertical">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              className="dock-tab"
              data-tip={tab.label}
              data-tip-side="left"
              aria-label={tab.label}
              aria-selected={activeTab === tab.id}
              onClick={() => onTabChange(tab.id)}
            >
              <Icon name={tab.icon} size="md" />
              {tab.badge === "dot" && <span className="rail-badge dot" />}
              {typeof tab.badge === "number" && tab.badge > 0 && (
                <span className="rail-badge">{tab.badge > 99 ? "99+" : tab.badge}</span>
              )}
            </button>
          ))}
        </div>
      </aside>
      {!collapsed && resizer}
    </div>
  );
}

export function ChromeEmpty({
  icon,
  spin,
  children,
}: {
  icon: IconName;
  spin?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="chrome-empty">
      <Icon name={icon} size="xl" className={spin ? "spin" : undefined} />
      <p>{children}</p>
    </div>
  );
}
