"use client";

import {
  ChevronsUpDown,
  InfoIcon,
  Settings2Icon,
  SettingsIcon,
  UserPlusIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar";
import { useAuth } from "@/core/auth/AuthProvider";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

import { useSettingsDialog } from "./settings";

function NavMenuButtonContent({
  isSidebarOpen,
  t,
}: {
  isSidebarOpen: boolean;
  t: ReturnType<typeof useI18n>["t"];
}) {
  return isSidebarOpen ? (
    <div className="flex w-full items-center gap-2 text-left text-sm text-[color:var(--neumo-text-muted)]">
      <SettingsIcon className="size-4" />
      <span>{t.workspace.settingsAndMore}</span>
      <ChevronsUpDown className="ml-auto size-4 text-[color:var(--neumo-text-subtle)]" />
    </div>
  ) : (
    <div className="flex size-full items-center justify-center">
      <SettingsIcon className="size-4 text-[color:var(--neumo-text-muted)]" />
    </div>
  );
}

export function WorkspaceNavMenu() {
  const { openSettings } = useSettingsDialog();
  const [mounted, setMounted] = useState(false);
  const { open: isSidebarOpen } = useSidebar();
  const { t } = useI18n();
  const { user } = useAuth();

  useEffect(() => {
    setMounted(true);
  }, []);

  return (
    <SidebarMenu className="w-full px-2">
      <SidebarMenuItem>
        {mounted ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <SidebarMenuButton
                size="lg"
                className={cn(
                  "rounded-2xl transition-all duration-300 ease-in-out",
                  "neumo-raised-sm text-[color:var(--neumo-text-muted)] hover:text-[color:var(--neumo-text)]",
                  "data-[state=open]:neumo-inset-sm",
                )}
              >
                <NavMenuButtonContent isSidebarOpen={isSidebarOpen} t={t} />
              </SidebarMenuButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              className="neumo-raised w-(--radix-dropdown-menu-trigger-width) min-w-56 rounded-2xl p-2"
              align="end"
              sideOffset={8}
            >
              <DropdownMenuGroup className="space-y-1">
                <DropdownMenuItem
                  className="rounded-xl px-3 py-2 text-sm text-[color:var(--neumo-text)] focus:bg-[color:var(--neumo-bg-soft)] focus:text-[color:var(--neumo-text)]"
                  onClick={() => {
                    openSettings("appearance");
                  }}
                >
                  <Settings2Icon />
                  {t.common.settings}
                </DropdownMenuItem>
                {user?.system_role === "admin" && (
                  <DropdownMenuItem asChild>
                    <a
                      href="/workspace/admin/invitations"
                      className="rounded-xl px-3 py-2 text-sm text-[color:var(--neumo-text)] focus:bg-[color:var(--neumo-bg-soft)] focus:text-[color:var(--neumo-text)]"
                    >
                      <UserPlusIcon />
                      用户邀请
                    </a>
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
              <DropdownMenuSeparator className="bg-[color:var(--neumo-shadow)]/40" />
              <DropdownMenuItem
                className="rounded-xl px-3 py-2 text-sm text-[color:var(--neumo-text)] focus:bg-[color:var(--neumo-bg-soft)]"
                onClick={() => {
                  openSettings("about");
                }}
              >
                <InfoIcon />
                {t.workspace.about}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          <SidebarMenuButton
            size="lg"
            className="neumo-raised-sm pointer-events-none rounded-2xl"
          >
            <NavMenuButtonContent isSidebarOpen={isSidebarOpen} t={t} />
          </SidebarMenuButton>
        )}
      </SidebarMenuItem>
    </SidebarMenu>
  );
}
