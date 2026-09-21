import { useEffect, useState } from "react";

/**
 * Dark/light toggle.
 *
 * Applied by setting `data-theme` on `<html>` before paint (an inline script in the
 * layout), so a reload never flashes the wrong theme. The preference is the only thing
 * persisted, and it is a preference — not analytics.
 */
const STORAGE_KEY = "terrasentinel-theme";

export default function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  useEffect(() => {
    const current = document.documentElement.dataset.theme;
    setTheme(current === "light" ? "light" : "dark");
  }, []);

  function toggle() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // A blocked storage API must not break the toggle.
    }
  }

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-muted)] transition-colors hover:text-[var(--color-foreground)]"
    >
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}
