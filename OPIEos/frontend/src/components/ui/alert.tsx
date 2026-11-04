import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const alertVariants = cva(
  // Alerts live in a recessed inset well so the user reads them as a
  // different surface layer — important for danger / warning copy.
  "neumo-inset-sm relative w-full rounded-2xl border border-transparent px-4 py-3 text-sm grid has-[>svg]:grid-cols-[calc(var(--spacing)*4)_1fr] grid-cols-[0_1fr] has-[>svg]:gap-x-3 gap-y-0.5 items-start [&>svg]:size-4 [&>svg]:text-current",
  {
    variants: {
      variant: {
        default:
          "text-[var(--neumo-text)] [&_p]:text-[var(--neumo-text-muted)]",
        destructive:
          "text-[var(--neumo-danger)] [&_p]:text-[var(--neumo-danger)] [&>svg]:text-[var(--neumo-danger)]",
        info: "text-[var(--neumo-info)] [&_p]:text-[var(--neumo-info)] [&>svg]:text-[var(--neumo-info)]",
        warning:
          "text-[var(--neumo-warning)] [&_p]:text-[var(--neumo-warning)] [&>svg]:text-[var(--neumo-warning)]",
        success:
          "text-[var(--neumo-success)] [&_p]:text-[var(--neumo-success)] [&>svg]:text-[var(--neumo-success)]",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Alert({
  className,
  variant,
  ...props
}: React.ComponentProps<"div"> & VariantProps<typeof alertVariants>) {
  return (
    <div
      data-slot="alert"
      role="alert"
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  );
}

function AlertTitle({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-title"
      className={cn(
        "col-start-2 line-clamp-1 min-h-4 font-medium tracking-tight",
        className,
      )}
      {...props}
    />
  );
}

function AlertDescription({
  className,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="alert-description"
      className={cn(
        "text-muted-foreground col-start-2 grid justify-items-start gap-1 text-sm [&_p]:leading-relaxed",
        className,
      )}
      {...props}
    />
  );
}

export { Alert, AlertTitle, AlertDescription };
