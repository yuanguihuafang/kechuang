"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo } from "react";

import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

export function WorkspaceContainer({
  className,
  children,
  ...props
}: React.ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "neumo-surface flex h-screen w-full flex-col text-[color:var(--neumo-text)]",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export function WorkspaceHeader({
  className,
  children,
  ...props
}: React.ComponentProps<"header">) {
  const { t } = useI18n();
  const pathname = usePathname();
  const segments = useMemo(() => {
    const parts = pathname?.split("/") || [];
    if (parts.length > 0) {
      return parts.slice(1, 3);
    }
  }, [pathname]);
  return (
    <header
      className={cn(
        "top-4 right-4 z-20 mx-auto flex h-12 max-w-full items-center justify-between gap-2 rounded-2xl px-4 transition-[width,height] ease-out md:px-5",
        "neumo-raised-sm",
        className,
      )}
      {...props}
    >
      <div className="flex min-w-0 items-center gap-2">
        <SidebarTrigger className="neumo-btn size-8 rounded-xl p-0 text-[color:var(--neumo-text)] md:hidden" />
        <Breadcrumb>
          <BreadcrumbList>
            {segments?.[0] && (
              <BreadcrumbItem className="hidden md:block">
                <BreadcrumbLink asChild>
                  <Link
                    href={`/${segments[0]}`}
                    className="text-[color:var(--neumo-text-muted)] transition-colors hover:text-[color:var(--neumo-text)]"
                  >
                    {nameOfSegment(segments[0], t)}
                  </Link>
                </BreadcrumbLink>
              </BreadcrumbItem>
            )}
            {segments?.[1] && (
              <>
                <BreadcrumbSeparator className="hidden md:block" />
                <BreadcrumbItem>
                  {segments.length >= 2 ? (
                    <BreadcrumbLink asChild>
                      <Link
                        href={`/${segments[0]}/${segments[1]}`}
                        className="text-[color:var(--neumo-text-muted)] transition-colors hover:text-[color:var(--neumo-text)]"
                      >
                        {nameOfSegment(segments[1], t)}
                      </Link>
                    </BreadcrumbLink>
                  ) : (
                    <BreadcrumbPage className="font-semibold text-[color:var(--neumo-text)]">
                      {nameOfSegment(segments[1], t)}
                    </BreadcrumbPage>
                  )}
                </BreadcrumbItem>
              </>
            )}
            {children && (
              <>
                <BreadcrumbSeparator />
                {children}
              </>
            )}
          </BreadcrumbList>
        </Breadcrumb>
      </div>
    </header>
  );
}

export function WorkspaceBody({
  className,
  children,
  ...props
}: React.ComponentProps<"main">) {
  return (
    <main
      className={cn(
        "relative flex min-h-0 w-full flex-1 flex-col items-center pt-4",
        className,
      )}
      {...props}
    >
      <div className="flex h-full w-full flex-col items-center">{children}</div>
    </main>
  );
}

function nameOfSegment(
  segment: string | undefined,
  t: ReturnType<typeof useI18n>["t"],
) {
  if (!segment) return t.common.home;
  if (segment === "workspace") return t.breadcrumb.workspace;
  if (segment === "chats") return t.breadcrumb.chats;
  return segment[0]?.toUpperCase() + segment.slice(1);
}
