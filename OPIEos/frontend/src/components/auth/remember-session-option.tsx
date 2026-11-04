"use client";

import { useI18n } from "@/core/i18n/hooks";

interface RememberSessionOptionProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
}

export function RememberSessionOption({
  checked,
  onCheckedChange,
}: RememberSessionOptionProps) {
  const { t } = useI18n();

  return (
    <label className="flex items-start gap-3 text-sm text-[color:var(--neumo-text-muted)]">
      <button
        type="button"
        role="checkbox"
        aria-checked={checked}
        onClick={() => onCheckedChange(!checked)}
        className={
          "mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-md transition-all duration-300 ease-in-out " +
          (checked
            ? "neumo-inset-sm text-[color:var(--neumo-accent-text)]"
            : "neumo-raised-sm text-transparent")
        }
      >
        <svg
          viewBox="0 0 20 20"
          fill="currentColor"
          className="size-3"
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            d="M16.704 5.292a1 1 0 010 1.416l-7.5 7.5a1 1 0 01-1.416 0l-3.5-3.5a1 1 0 011.416-1.416L8.5 12.08l6.788-6.788a1 1 0 011.416 0z"
            clipRule="evenodd"
          />
        </svg>
      </button>
      <span>
        <span className="block font-medium text-[color:var(--neumo-text)]">
          {t.login.rememberMe}
        </span>
        <span className="text-[color:var(--neumo-text-subtle)]">
          {t.login.rememberMeDescription}
        </span>
      </span>
    </label>
  );
}
