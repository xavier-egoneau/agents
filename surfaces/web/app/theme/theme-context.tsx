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
import { baseIcons, themeIconSets, type IconName } from "./icons";

type ThemeContextValue = {
  theme: ThemeId;
  setTheme: (theme: ThemeId) => void;
};

const ThemeContext = createContext<ThemeContextValue>({
  theme: DEFAULT_THEME,
  setTheme: () => {},
});

export function ThemeProvider({ children }: { children: ReactNode }) {
  // The server and the first client render must agree. The bootstrap script
  // already applies colors before paint; React synchronizes its icon registry
  // only after hydration.
  const [theme, setThemeState] = useState<ThemeId>(DEFAULT_THEME);

  useEffect(() => {
    const applied = document.documentElement.dataset.theme;
    // Hydration must start with the server default; synchronize only after it.
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
  // Accès direct au registre plutôt qu'appel de fonction : `resolveIcon()`
  // renvoie un composant existant, mais un appel dans le corps du rendu est
  // indiscernable d'une création de composant — laquelle réinitialiserait
  // l'état du sous-arbre à chaque rendu.
  const Glyph = themeIconSets[theme]?.[name] ?? baseIcons[name];
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
