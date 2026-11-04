import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  // Buttons by default render on the same clay surface as the page. Each
  // variant tweaks the *color* and shadow depth while obeying the
  // extrude -> intrude rule (raised -> hover shrinks -> active is inset).
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-xl text-sm font-medium transition-all duration-300 ease-in-out disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg:not([class*='size-'])]:size-4 shrink-0 [&_svg]:shrink-0 outline-none focus-visible:ring-2 focus-visible:ring-[var(--neumo-accent)]/40 aria-invalid:ring-destructive/30",
  {
    variants: {
      variant: {
        default: "cursor-pointer neumo-btn text-[var(--neumo-text)]",
        destructive:
          "cursor-pointer neumo-btn text-[var(--neumo-danger)] aria-[pressed=true]:text-white",
        accent: "cursor-pointer neumo-btn-accent",
        outline: "cursor-pointer neumo-btn",
        secondary: "cursor-pointer neumo-btn text-[var(--neumo-text)]",
        ghost:
          "cursor-pointer text-[var(--neumo-text-muted)] hover:text-[var(--neumo-text)] hover:neumo-inset-sm bg-transparent rounded-xl transition-all duration-300 ease-in-out",
        link: "cursor-pointer text-[var(--neumo-accent)] underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-4 py-2 has-[>svg]:px-3",
        sm: "h-8 rounded-xl gap-1.5 px-3 has-[>svg]:px-2.5",
        lg: "h-11 rounded-2xl px-6 has-[>svg]:px-4 text-base",
        icon: "size-9",
        "icon-sm": "size-8",
        "icon-lg": "size-10",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  }) {
  const Comp = asChild ? Slot : "button";

  return (
    <Comp
      data-slot="button"
      {...(variant !== undefined && { "data-variant": variant })}
      {...(size !== undefined && { "data-size": size })}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  );
}

export { Button, buttonVariants };
