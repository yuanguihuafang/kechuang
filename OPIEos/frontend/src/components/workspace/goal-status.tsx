"use client";

import { RefreshCwIcon, TargetIcon } from "lucide-react";

import { useI18n } from "@/core/i18n/hooks";
import type { GoalState } from "@/core/threads";
import { cn } from "@/lib/utils";

import { getGoalContinuationDisplay } from "./goal-status-helpers";
import { Tooltip } from "./tooltip";

export function GoalStatus({
  className,
  goal,
}: {
  className?: string;
  goal: GoalState;
}) {
  const { t } = useI18n();
  const continuation = getGoalContinuationDisplay(goal);
  return (
    <div
      className={cn(
        "neumo-inset-sm flex min-h-10 w-full items-center gap-3 rounded-t-2xl border-0 px-4 py-2 text-sm text-[var(--neumo-text)]",
        className,
      )}
    >
      <TargetIcon className="size-4 shrink-0 text-[var(--neumo-accent-text)]" />
      <div className="min-w-0 flex-1 truncate">
        <span className="text-muted-foreground mr-2">
          {t.inputBox.goalLabel}
        </span>
        <span className="font-medium">{goal.objective}</span>
      </div>
      {continuation && (
        <Tooltip
          content={t.inputBox.goalContinuationTooltip
            .replace("{count}", String(continuation.count))
            .replace("{max}", String(continuation.max))}
        >
          <span className="text-muted-foreground flex shrink-0 items-center gap-1 text-xs tabular-nums">
            <RefreshCwIcon className="size-3" />
            {t.inputBox.goalContinuing
              .replace("{count}", String(continuation.count))
              .replace("{max}", String(continuation.max))}
          </span>
        </Tooltip>
      )}
    </div>
  );
}
