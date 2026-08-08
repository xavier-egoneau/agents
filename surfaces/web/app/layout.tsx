import type { Metadata } from "next";
import "./globals.css";
import "./theme/fonts.css";
import { ThemeProvider } from "./theme/theme-context";
import { DEFAULT_THEME, THEME_BOOTSTRAP_SCRIPT } from "./theme/registry";

/**
 * Les polices sont servies depuis `public/fonts/`, pas par `next/font/google`.
 *
 * Sur Windows, vinext écrit `url(C:/Users/…/.vinext/fonts/…)` dans le CSS
 * généré : le navigateur y voit une URL `file://` et refuse de la charger, si
 * bien que la police n'arrivait jamais. Il produisait par ailleurs onze
 * fichiers — cyrillique, grec, vietnamien — malgré `subsets: ["latin"]`.
 *
 * Deux fichiers latins de 52 Ko au total suffisent, et ne dépendent plus de la
 * plateforme ni de la version de l'outil.
 */

export const metadata: Metadata = {
  title: "AMK — Agentic Markdown Kernel",
  description: "Pilotez vos agents, skills et délégations depuis une surface claire.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // `suppressHydrationWarning` est ici volontaire et limité à cet attribut :
    // le script ci-dessous applique le thème mémorisé avant le premier paint,
    // donc l'HTML servi et le DOM hydraté diffèrent forcément. Sans cela, React
    // signale un écart à chaque chargement pour un comportement voulu — et le
    // bruit finit par masquer les vrais écarts d'hydratation.
    <html lang="fr" data-theme={DEFAULT_THEME} suppressHydrationWarning>
      <head>
        {/* Applique le thème mémorisé avant le premier paint : évite tout flash. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP_SCRIPT }} />
      </head>
      <body
        className="antialiased"
      >
        <ThemeProvider>{children}</ThemeProvider>
      </body>
    </html>
  );
}
