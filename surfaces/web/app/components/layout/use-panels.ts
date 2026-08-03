"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

export type PanelKey = "left" | "right";

type PanelState = { open: boolean; width: number };

type Bounds = { min: number; max: number; initial: number };

const STORAGE_KEY = "amk.panels";

const BOUNDS: Record<PanelKey, Bounds> = {
  left: { min: 208, max: 460, initial: 288 },
  right: { min: 300, max: 900, initial: 520 },
};

function clamp(value: number, { min, max }: Bounds) {
  return Math.min(max, Math.max(min, Math.round(value)));
}

function readStored(): Partial<Record<PanelKey, PanelState>> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Partial<Record<PanelKey, PanelState>>) : {};
  } catch {
    return {};
  }
}

/**
 * Gere l'ouverture et la largeur des deux panneaux du shell.
 * Les largeurs sont persistees et le drag passe par les Pointer Events, ce qui
 * couvre souris, trackpad et tactile avec un seul chemin de code.
 */
export function usePanels() {
  const [left, setLeft] = useState<PanelState>({ open: true, width: BOUNDS.left.initial });
  const [right, setRight] = useState<PanelState>({ open: false, width: BOUNDS.right.initial });
  const [resizing, setResizing] = useState<PanelKey | null>(null);
  const drag = useRef<{ key: PanelKey; startX: number; startWidth: number } | null>(null);

  useEffect(() => {
    const stored = readStored();
    if (stored.left) {
      const width = clamp(stored.left.width, BOUNDS.left);
      setLeft({ open: stored.left.open, width });
    }
    if (stored.right) {
      setRight((current) => ({ ...current, width: clamp(stored.right!.width, BOUNDS.right) }));
    }
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ left, right }));
    } catch {
      /* stockage indisponible : la largeur reste valable pour la session */
    }
  }, [left, right]);

  const startResize = useCallback(
    (key: PanelKey) => (event: ReactPointerEvent<HTMLElement>) => {
      event.preventDefault();
      const startWidth = key === "left" ? left.width : right.width;
      drag.current = { key, startX: event.clientX, startWidth };
      setResizing(key);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [left.width, right.width],
  );

  useEffect(() => {
    if (!resizing) return;

    const move = (event: PointerEvent) => {
      const state = drag.current;
      if (!state) return;
      const delta = event.clientX - state.startX;
      // Le panneau droit grandit vers la gauche : le delta est inverse.
      const next = clamp(
        state.key === "left" ? state.startWidth + delta : state.startWidth - delta,
        BOUNDS[state.key],
      );
      const apply = state.key === "left" ? setLeft : setRight;
      apply((current) => (current.width === next ? current : { ...current, width: next }));
    };

    const stop = () => {
      drag.current = null;
      setResizing(null);
    };

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
    };
  }, [resizing]);

  /** Ajustement au clavier : la poignee est focusable et repond aux fleches. */
  const nudge = useCallback(
    (key: PanelKey) => (event: ReactKeyboardEvent<HTMLElement>) => {
      const step = event.shiftKey ? 40 : 12;
      let delta = 0;
      if (event.key === "ArrowLeft") delta = key === "left" ? -step : step;
      else if (event.key === "ArrowRight") delta = key === "left" ? step : -step;
      else return;
      event.preventDefault();
      const apply = key === "left" ? setLeft : setRight;
      apply((current) => ({ ...current, width: clamp(current.width + delta, BOUNDS[key]) }));
    },
    [],
  );

  const toggle = useCallback((key: PanelKey) => {
    const apply = key === "left" ? setLeft : setRight;
    apply((current) => ({ ...current, open: !current.open }));
  }, []);

  const setOpen = useCallback((key: PanelKey, open: boolean) => {
    const apply = key === "left" ? setLeft : setRight;
    apply((current) => (current.open === open ? current : { ...current, open }));
  }, []);

  return { left, right, resizing, startResize, nudge, toggle, setOpen };
}
