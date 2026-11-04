"use client";

import * as React from "react";
import * as SwitchPrimitive from "@radix-ui/react-switch";

import { cn } from "@/lib/utils";

function Switch({
  className,
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        // Switch track is a recessed well. Off state is plain inset; on
        // state inverts the well to a tinted raised accent so the user
        // can read the state at a glance.
        "peer neumo-inset inline-flex h-6 w-11 shrink-0 items-center rounded-full border border-transparent transition-all duration-300 ease-in-out outline-none focus-visible:ring-2 focus-visible:ring-[var(--neumo-accent)]/40 disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-[var(--neumo-accent)] data-[state=unchecked]:bg-[var(--neumo-bg)]",
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          "pointer-events-neumo block size-5 rounded-full bg-[var(--neumo-bg)] shadow-[2px_2px_4px_var(--neumo-shadow),-2px_-2px_4px_var(--neumo-highlight)] ring-0 transition-all duration-300 ease-in-out data-[state=checked]:translate-x-5 data-[state=unchecked]:translate-x-0.5",
        )}
      />
    </SwitchPrimitive.Root>
  );
}

export { Switch };
