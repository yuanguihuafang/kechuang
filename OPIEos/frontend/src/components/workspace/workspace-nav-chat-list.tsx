"use client";

import { BotIcon, CalendarClock, MessagesSquare } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import {
  SidebarGroup,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useAgentsApiEnabled } from "@/core/agents";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

function navItemClass(isActive: boolean) {
  return cn(
    "rounded-2xl transition-all duration-300 ease-in-out",
    isActive
      ? "neumo-inset-sm text-[color:var(--neumo-accent-text)]"
      : "neumo-raised-sm text-[color:var(--neumo-text-muted)] hover:text-[color:var(--neumo-text)]",
  );
}

export function WorkspaceNavChatList() {
  const { t } = useI18n();
  const pathname = usePathname();
  const { enabled: agentsEnabled } = useAgentsApiEnabled();
  return (
    <SidebarGroup className="px-3 pt-2">
      <SidebarMenu className="gap-2">
        <SidebarMenuItem>
          <SidebarMenuButton
            isActive={pathname === "/workspace/chats"}
            asChild
            className={navItemClass(pathname === "/workspace/chats")}
          >
            <Link href="/workspace/chats" className="flex items-center gap-2">
              <MessagesSquare />
              <span>{t.sidebar.chats}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
        <SidebarMenuItem>
          {agentsEnabled ? (
            <SidebarMenuButton
              isActive={pathname.startsWith("/workspace/agents")}
              asChild
              className={navItemClass(pathname.startsWith("/workspace/agents"))}
            >
              <Link
                href="/workspace/agents"
                className="flex items-center gap-2"
              >
                <BotIcon />
                <span>{t.sidebar.agents}</span>
              </Link>
            </SidebarMenuButton>
          ) : (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="block w-full cursor-not-allowed">
                  <SidebarMenuButton
                    className={cn(
                      navItemClass(false),
                      "text-[color:var(--neumo-text-subtle)] opacity-70",
                    )}
                    aria-disabled
                    aria-describedby="agents-disabled-reason"
                  >
                    <BotIcon />
                    <span>{t.sidebar.agents}</span>
                  </SidebarMenuButton>
                  <span id="agents-disabled-reason" className="sr-only">
                    {t.sidebar.agentsDisabledTooltip}
                  </span>
                </span>
              </TooltipTrigger>
              <TooltipContent side="right">
                {t.sidebar.agentsDisabledTooltip}
              </TooltipContent>
            </Tooltip>
          )}
        </SidebarMenuItem>
        <SidebarMenuItem>
          <SidebarMenuButton
            isActive={pathname.startsWith("/workspace/scheduled-tasks")}
            asChild
            className={navItemClass(
              pathname.startsWith("/workspace/scheduled-tasks"),
            )}
          >
            <Link
              href="/workspace/scheduled-tasks"
              className="flex items-center gap-2"
            >
              <CalendarClock />
              <span>{t.sidebar.scheduledTasks}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarGroup>
  );
}
