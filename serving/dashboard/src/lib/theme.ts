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

function read(): Theme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/**
 * Read and write the theme.
 *
 * The initial value is read in the state initialiser rather than in an effect. An effect
 * that calls `setState` on mount forces every island that uses this hook through a second
 * render, and when Astro mounts several islands in one task that update can land while a
 * neighbouring island is mid-render, which React reports as "triggering nested component
 * updates from render" and answers by skipping a commit. These components are all
 * `client:only`, so reading the document during the first render is safe.
 *
 * The `MutationObserver` is there for a different reason: the theme can change from
 * outside this hook, and observing the attribute means the map's basemap follows the page
 * even when whatever changed it is not this component.
 */
export function useTheme(): [Theme, (next: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(read);

  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(read()));
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
