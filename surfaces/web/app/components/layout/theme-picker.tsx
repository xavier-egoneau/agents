"use client";

import { useEffect, useRef, useState } from "react";
import { Icon, useTheme } from "../../theme/theme-context";
import { THEMES } from "../../theme/registry";

/**
 * Selecteur de theme. Utile en soi, mais surtout : c'est la preuve que la
 * surcouche fonctionne — changer d'entree ici ne touche ni au HTML ni au CSS
 * de base, uniquement l'attribut data-theme.
 */
/**
 * `placement` positionne le menu : "rail" l'ouvre à droite du rail étroit,
 * "panel" au-dessus et aligné à droite — sinon il sortirait du panneau, qui
 * est en overflow:hidden pour permettre le repli.
 */
export function ThemePicker({ placement = "rail" }: { placement?: "rail" | "panel" }) {
  const { theme, setTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  return (
    <div ref={root} style={{ position: "relative" }}>
      <button
        type="button"
        className={placement === "rail" ? "rail-item" : "ibtn sm"}
        data-tip="Thème"
        data-tip-side={placement === "rail" ? undefined : "bottom-end"}
        aria-label="Changer de thème"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="theme" size="md" />
      </button>

      {open && (
        <div className={`theme-menu ${placement}`} role="menu">
          {THEMES.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="menuitemradio"
              aria-checked={theme === entry.id}
              className="theme-option"
              onClick={() => {
                setTheme(entry.id);
                setOpen(false);
              }}
            >
              <span className="theme-swatch" aria-hidden="true">
                {entry.swatch.map((color) => (
                  <i key={color} style={{ background: color }} />
                ))}
              </span>
              <span>
                <strong>{entry.label}</strong>
                <small>{entry.description}</small>
              </span>
              {theme === entry.id && <Icon name="success" size="sm" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
