"use client";

import { BotOffIcon } from "lucide-react";

import { useI18n } from "@/core/i18n/hooks";

export function AgentsFeatureDisabled() {
  const { t } = useI18n();
  return (
    <div className="flex size-full flex-col items-center justify-center gap-3 p-6 text-center">
      <div className="neumo-raised-sm flex h-14 w-14 items-center justify-center rounded-full">
        <BotOffIcon className="h-7 w-7 text-[var(--neumo-text-subtle)]" />
      </div>
      <div>
        <p className="font-medium text-[var(--neumo-text)]">
          {t.agents.featureDisabledTitle}
        </p>
        <p className="mt-1 max-w-md text-sm text-[var(--neumo-text-muted)]">
          {t.agents.featureDisabledDescription}
        </p>
      </div>
    </div>
  );
}
