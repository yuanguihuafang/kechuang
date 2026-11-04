import * as React from "react";

import { cn } from "@/lib/utils";

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        // Inputs by default sit in a recessed well on the same clay
        // surface. Focus slightly lightens the inset — channels open,
        // does not press harder.
        "neumo-input h-9 w-full min-w-0 rounded-xl border-transparent bg-transparent px-3 py-1 text-sm outline-none selection:bg-[var(--neumo-accent)]/40 selection:text-[var(--neumo-text)] file:inline-flex file:h-7 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-[var(--neumo-text)] placeholder:text-[var(--neumo-text-subtle)] disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
        "aria-[invalid=true]:shadow-[inset_3px_3px_6px_rgba(239,68,68,0.35),inset_-3px_-3px_6px_var(--neumo-highlight)]",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
