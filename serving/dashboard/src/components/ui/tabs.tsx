import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface TabItem<T extends string> {
  value: T;
  label: ReactNode;
  hint?: string;
}

/**
 * A controlled tab strip built from real buttons with `aria-selected`, so it is
 * announced correctly and reachable by keyboard without a library.
 */
export function Tabs<T extends string>({
  items,
  value,
  onChange,
  className,
  size = "md",
}: {
  items: TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
  size?: "sm" | "md";
}) {
  return (
    <div role="tablist" className={cn("inline-flex rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-0.5", className)}>
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            role="tab"
            type="button"
            aria-selected={active}
            title={item.hint}
            onClick={() => onChange(item.value)}
            className={cn(
              "rounded-md font-medium transition-colors",
              size === "sm" ? "px-2 py-1 text-[11px]" : "px-2.5 py-1.5 text-xs",
              active
                ? "bg-[var(--color-surface-raised)] text-[var(--color-foreground)] shadow-sm"
                : "text-[var(--color-muted)] hover:text-[var(--color-foreground)]",
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
