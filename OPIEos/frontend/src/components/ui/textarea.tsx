import * as React from "react";

import { cn } from "@/lib/utils";

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        // Textarea lives inside a recessed well; selection and focus
        // follow the same rules as Input. We omit the file: variant
        // selector since textareas don't host file inputs.
        "neumo-input flex field-sizing-content min-h-16 w-full rounded-xl border-transparent bg-transparent px-3 py-2 text-sm outline-none placeholder:text-[var(--neumo-text-subtle)] disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
        className,
      )}
      {...props}
    />
  );
}

export { Textarea };
