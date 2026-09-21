import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/utils";

/**
 * shadcn/ui's button, vendored. Variants are declared once with cva so a page cannot
 * invent a fifteenth shade of grey, and `focus-visible` comes from the global token
 * rather than being re-specified per usage.
 */
export const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        default: "bg-[var(--color-accent)] text-[var(--color-accent-foreground)] hover:brightness-110",
        outline:
          "border border-[var(--color-border-strong)] text-[var(--color-foreground)] hover:bg-[var(--color-surface-raised)]",
        ghost: "text-[var(--color-muted)] hover:bg-[var(--color-surface-raised)] hover:text-[var(--color-foreground)]",
        subtle: "bg-[var(--color-surface-raised)] text-[var(--color-foreground)] hover:brightness-110",
      },
      size: {
        sm: "h-7 px-2.5 text-xs",
        md: "h-8 px-3",
        lg: "h-9 px-4",
        icon: "h-8 w-8",
      },
    },
    defaultVariants: { variant: "outline", size: "sm" },
  },
);

export function Button({
  className,
  variant,
  size,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & VariantProps<typeof buttonVariants>) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
