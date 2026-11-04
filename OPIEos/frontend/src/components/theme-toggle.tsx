"use client";

import {
  MonitorSmartphoneIcon,
  MoonIcon,
  SunIcon,
  type LucideIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

type ThemeChoice = "light" | "dark" | "system";

const CHOICES: { id: ThemeChoice; label: string; icon: LucideIcon }[] = [
  { id: "light", label: "浅色", icon: SunIcon },
  { id: "dark", label: "深色", icon: MoonIcon },
  { id: "system", label: "跟随系统", icon: MonitorSmartphoneIcon },
];

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme, resolvedTheme } = useTheme();
  // Avoid hydration mismatch — next-themes only knows the resolved theme
  // after mount.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // Keep the first client render identical to SSR. next-themes resolves the
  // persisted/system theme only after mount, so reading `theme` immediately
  // would make the icon differ between server and client.
  const current: ThemeChoice = mounted
    ? ((theme ?? "system") as ThemeChoice)
    : "system";
  const ActiveIcon =
    CHOICES.find((choice) => choice.id === current)?.icon ??
    MonitorSmartphoneIcon;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label="切换主题"
          className={className}
        >
          <ActiveIcon className="size-4" />
          <span className="sr-only">切换主题</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        <DropdownMenuLabel className="text-xs font-normal text-[color:var(--neumo-text-muted)]">
          主题
          {mounted && resolvedTheme ? (
            <span className="ml-1 text-[color:var(--neumo-text-subtle)]">
              · 当前 {resolvedTheme === "dark" ? "深色" : "浅色"}
            </span>
          ) : null}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {CHOICES.map((choice) => {
          const Icon = choice.icon;
          return (
            <DropdownMenuItem
              key={choice.id}
              onSelect={() => setTheme(choice.id)}
              className="flex items-center gap-2"
            >
              <Icon className="size-4" />
              <span>{choice.label}</span>
              {current === choice.id ? (
                <span className="ml-auto text-xs text-[color:var(--neumo-accent-text)]">
                  •
                </span>
              ) : null}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
