"use client";

import { MenuIcon } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { OpieLogo } from "@/components/brand/opie-logo";
import { ThemeToggle } from "@/components/theme-toggle";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";

export type MobileNavLink = {
  href: string;
  label: string;
};

export function MobileNav({
  links,
  ctaHref,
}: {
  links: MobileNavLink[];
  ctaHref: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <div className="flex items-center gap-1 sm:hidden">
        <ThemeToggle className="neumo-btn size-9 rounded-xl p-0 text-[color:var(--neumo-text-muted)] hover:text-[color:var(--neumo-text)]" />
        <SheetTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Open menu"
            className="neumo-btn rounded-xl text-[color:var(--neumo-text)] hover:bg-transparent"
          >
            <MenuIcon className="size-5" />
          </Button>
        </SheetTrigger>
      </div>
      <SheetContent
        side="right"
        className="neumo-surface w-72 border-0 px-4 text-[color:var(--neumo-text)]"
      >
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2 text-lg font-semibold">
            <OpieLogo className="size-6" />
            OPIE&nbsp;OS
          </SheetTitle>
        </SheetHeader>
        <div className="mt-6 flex items-center justify-between rounded-xl py-1">
          <span className="text-xs font-medium tracking-wide text-[color:var(--neumo-text-muted)] uppercase">
            主题
          </span>
          <ThemeToggle className="neumo-btn size-9 rounded-xl p-0 text-[color:var(--neumo-text-muted)] hover:text-[color:var(--neumo-text)]" />
        </div>
        <nav className="mt-4 flex flex-col gap-2 text-sm font-medium">
          {links.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              onClick={() => setOpen(false)}
              className="neumo-btn rounded-xl px-3 py-3 text-[color:var(--neumo-text-muted)]"
            >
              {link.label}
            </Link>
          ))}
          <Link
            href={ctaHref}
            onClick={() => setOpen(false)}
            className="neumo-btn-accent mt-3 rounded-xl px-3 py-3 text-center text-white"
          >
            立即开始
          </Link>
        </nav>
      </SheetContent>
    </Sheet>
  );
}
