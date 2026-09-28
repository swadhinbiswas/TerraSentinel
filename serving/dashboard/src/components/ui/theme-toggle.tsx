import { useTheme } from "@/lib/theme";

/**
 * Dark/light toggle.
 *
 * The theme is applied by setting `data-theme` on `<html>` (from an inline script in the
 * layout, before first paint), so a reload never flashes the wrong theme. `useTheme` owns
 * the storage key and the attribute, so this component is only the button.
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useTheme();

  return (
    <button
      type="button"
      onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      className="rounded-md border border-[var(--color-border)] px-2 py-1 text-xs text-[var(--color-muted)] transition-colors hover:text-[var(--color-foreground)]"
    >
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}
