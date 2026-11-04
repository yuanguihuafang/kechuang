"use client";

import { MessageSquarePlus } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { OpieLogo } from "@/components/brand/opie-logo";
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

export function WorkspaceHeader({ className }: { className?: string }) {
  const { t } = useI18n();
  const { state } = useSidebar();
  const pathname = usePathname();
  const isCollapsed = state === "collapsed";
  return (
    <>
      <div
        className={cn(
          "group/workspace-header flex h-14 flex-col justify-center",
          className,
        )}
      >
        {isCollapsed ? (
          <div className="flex w-full flex-col items-center gap-2">
            <Link href="/" aria-label="返回首页">
              <OpieLogo className="size-5 text-[color:var(--neumo-accent-text)]" />
            </Link>
            <SidebarTrigger className="neumo-btn size-7 rounded-lg p-0 text-[color:var(--neumo-text)]" />
          </div>
        ) : (
          <div className="neumo-raised-sm mx-2 flex items-center justify-between gap-2 rounded-2xl px-3 py-2">
            <Link
              href="/"
              aria-label="返回首页"
              className="flex items-center gap-1.5 font-serif font-semibold text-[color:var(--neumo-text)]"
            >
                <OpieLogo className="size-4 text-[color:var(--neumo-accent-text)]" />
                OPIE OS
            </Link>
            <SidebarTrigger className="neumo-btn size-7 rounded-lg p-0 text-[color:var(--neumo-text)]" />
          </div>
        )}
      </div>
      <SidebarMenu className="px-2 pt-2">
        <SidebarMenuItem>
          <SidebarMenuButton
            isActive={pathname === "/workspace/chats/new"}
            asChild
            className={cn(
              "rounded-2xl transition-all duration-300 ease-in-out",
              pathname === "/workspace/chats/new"
                ? "neumo-inset-sm text-[color:var(--neumo-accent-text)]"
                : "neumo-raised-sm text-[color:var(--neumo-text-muted)] hover:text-[color:var(--neumo-text)]",
            )}
          >
            <Link
              href="/workspace/chats/new"
              className="flex items-center gap-2"
            >
              <MessageSquarePlus size={16} />
              <span>{t.sidebar.newChat}</span>
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </>
  );
}
