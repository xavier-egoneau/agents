"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  DEFAULT_THEME,
  THEME_STORAGE_KEY,
  isThemeId,
  type ThemeId,
} from "./registry";
import { resolveIcon, type IconName } from "./icons";

type ThemeContextValue = {
  theme: ThemeId;
  setTheme: (theme: ThemeId) => void;
};

const ThemeContext = createContext<ThemeContextValue>({
  theme: DEFAULT_THEME,
  setTheme: () => {},
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeId>(DEFAULT_THEME);

  // Recupere le theme applique par le script de bootstrap (evite le flash).
  useEffect(() => {
    const applied = document.documentElement.dataset.theme;
    if (isThemeId(applied)) setThemeState(applied);
  }, []);

  const setTheme = useCallback((next: ThemeId) => {
    setThemeState(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      /* stockage indisponible : le theme reste valable pour la session */
    }
  }, []);

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  return useContext(ThemeContext);
}

type IconProps = {
  name: IconName;
  size?: "xs" | "sm" | "md" | "lg" | "xl";
  className?: string;
  strokeWidth?: number;
  "aria-hidden"?: boolean;
};

/**
 * Unique point d'entree pour afficher une icone. La taille et l'epaisseur
 * viennent des tokens, donc un theme peut les changer globalement.
 */
export function Icon({
  name,
  size = "md",
  className,
  strokeWidth,
  "aria-hidden": ariaHidden = true,
}: IconProps) {
  const { theme } = useTheme();
  const Glyph = resolveIcon(name, theme);
  return (
    <Glyph
      className={className}
      aria-hidden={ariaHidden}
      strokeWidth={strokeWidth}
      style={{
        width: `var(--icon-size-${size})`,
        height: `var(--icon-size-${size})`,
        strokeWidth: strokeWidth ?? "var(--icon-stroke)",
      }}
    />
  );
}
