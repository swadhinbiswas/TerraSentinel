import { useCallback, useEffect, useState } from "react";

/**
 * The single place that knows how the theme is stored and read.
 *
 * The layout sets `data-theme` on `<html>` from an inline script before first paint, so
 * the value is on the document by the time any island mounts. Two components need to
 * follow it — the toggle that writes it, and the map, whose basemap has to change with
 * it — and each keeping its own copy of the storage key is how they drift apart.
 */
export const THEME_KEY = "terrasentinel-theme";

export type Theme = "dark" | "light";

function current(): Theme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/**
 * Read and write the theme.
 *
 * The `MutationObserver` rather than a plain effect read, because the theme can change
 * from outside this hook: a second copy of the toggle, a browser extension, devtools.
 * Observing the attribute means the map's basemap follows the page even when the toggle
 * that changed it is a different component.
 */
export function useTheme(): [Theme, (next: Theme) => void] {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    setTheme(current());
    const observer = new MutationObserver(() => setTheme(current()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  const apply = useCallback((next: Theme) => {
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // A blocked storage API must not break the toggle.
    }
  }, []);

  return [theme, apply];
}
