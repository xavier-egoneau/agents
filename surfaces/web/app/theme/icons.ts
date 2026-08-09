/**
 * REGISTRY D'ICONES
 * -----------------------------------------------------------------------------
 * Le HTML ne reference JAMAIS une icone lucide directement : il demande un nom
 * semantique (`<Icon name="agent" />`). Un theme peut donc reassigner n'importe
 * quel nom vers une autre icone — voire une autre bibliotheque — sans qu'aucun
 * composant applicatif ne bouge.
 *
 * Regles :
 *   - lucide-react n'est importe QUE dans ce fichier ;
 *   - ajouter une icone = ajouter une cle dans `baseIcons` ;
 *   - un theme surcharge via `iconOverrides` (voir registry.ts).
 */

import {
  Volume2,
  VolumeX,
  Activity,
  ArrowUp,
  Bot,
  BookOpen,
  Braces,
  CalendarClock,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleStop,
  Clock,
  Copy,
  CornerDownLeft,
  Download,
  Ellipsis,
  FileCode,
  FileDiff,
  FileText,
  Folder,
  FolderOpen,
  FolderTree,
  Gauge,
  GitBranch,
  GitCommitHorizontal,
  History,
  Image as ImageIcon,
  Info,
  Layers,
  ListChecks,
  LoaderCircle,
  MessageSquarePlus,
  Palette,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  Paperclip,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  Send,
  Settings,
  Shield,
  Sparkles,
  Square,
  SquareTerminal,
  Trash2,
  TriangleAlert,
  Waypoints,
  Workflow,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";

/**
 * Catalogue semantique. La cle decrit un ROLE, jamais un dessin.
 * Mauvais : "calendar". Bon : "automation".
 */
export const baseIcons = {
  // Contexte / ressources
  project: FolderOpen,
  agent: Bot,
  skill: Sparkles,
  knowledge: BookOpen,
  provider: Layers,
  automation: CalendarClock,
  session: History,
  settings: Settings,
  theme: Palette,

  // Actions
  add: Plus,
  close: X,
  remove: Trash2,
  edit: Pencil,
  save: Save,
  copy: Copy,
  search: Search,
  refresh: RefreshCw,
  download: Download,
  attach: Paperclip,
  send: ArrowUp,
  submit: Send,
  stop: Square,
  play: Play,
  pause: Pause,
  more: Ellipsis,
  newChat: MessageSquarePlus,

  // Navigation / panneaux
  chevronRight: ChevronRight,
  chevronDown: ChevronDown,
  chevronLeft: ChevronLeft,
  enter: CornerDownLeft,
  panelLeftOpen: PanelLeftOpen,
  panelLeftClose: PanelLeftClose,
  panelRightOpen: PanelRightOpen,
  panelRightClose: PanelRightClose,

  // Git
  git: GitBranch,
  gitCommit: GitCommitHorizontal,
  diff: FileDiff,

  // Fichiers
  folder: Folder,
  folderOpen: FolderOpen,
  file: FileText,
  fileCode: FileCode,
  fileTree: FolderTree,
  image: ImageIcon,
  json: Braces,
  terminal: SquareTerminal,

  // Etats / feedback
  info: Info,
  warning: TriangleAlert,
  error: CircleAlert,
  success: CircleCheck,
  pending: Clock,
  running: LoaderCircle,
  cancelled: CircleStop,
  sound: Volume2,
  soundOff: VolumeX,
  activity: Activity,
  meter: Gauge,
  security: Shield,
  fast: Zap,

  // Plan / workflow
  plan: ListChecks,
  workflow: Workflow,
  trace: Waypoints,
} satisfies Record<string, LucideIcon>;

export type IconName = keyof typeof baseIcons;

export type IconSet = Partial<Record<IconName, LucideIcon>>;

/**
 * Surcharges par theme. Un theme peut ne reassigner qu'une poignee de roles ;
 * les autres retombent sur `baseIcons`.
 */
export const themeIconSets: Record<string, IconSet> = {
  editorial: {
    // Univers plus "documentaire" : le plan devient un jalonnement.
    plan: ListChecks,
    trace: Waypoints,
    session: History,
  },
};

/**
 * Résolution hors rendu React — génération statique, tests, scripts.
 *
 * Ne pas appeler dans le corps d'un composant : un appel de fonction renvoyant
 * un composant y est indiscernable d'une création de composant, ce qui
 * réinitialiserait l'état du sous-arbre à chaque rendu. Le composant `Icon`
 * accède au registre directement pour cette raison.
 */
export function resolveIcon(name: IconName, theme?: string): LucideIcon {
  const override = theme ? themeIconSets[theme]?.[name] : undefined;
  return override ?? baseIcons[name];
}
