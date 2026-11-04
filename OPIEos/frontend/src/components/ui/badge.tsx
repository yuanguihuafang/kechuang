import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center justify-center rounded-full border border-transparent px-2.5 py-0.5 text-xs font-medium w-fit whitespace-nowrap shrink-0 gap-1 transition-all duration-300 ease-in-out [&>svg]:size-3 [&_svg]:pointer-events-none overflow-hidden",
  {
    variants: {
      variant: {
        // Default sits in a subtle inset well — useful for status tags.
        default: "neumo-inset-sm text-[var(--neumo-text)]",
        secondary: "neumo-raised-sm text-[var(--neumo-text-muted)]",
        destructive: "neumo-raised-sm text-[var(--neumo-danger)]",
        accent: "neumo-btn-accent text-white",
        outline: "neumo-raised-sm text-[var(--neumo-text)]",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant,
  asChild = false,
  ...props
}: React.ComponentProps<"span"> &
  VariantProps<typeof badgeVariants> & { asChild?: boolean }) {
  const Comp = asChild ? Slot : "span";

  return (
    <Comp
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
