import Link from "next/link";

import { OpieLogo } from "@/components/brand/opie-logo";
import { ThemeToggle } from "@/components/theme-toggle";
import { getServerSideUser } from "@/core/auth/server";
import { buildLoginUrl } from "@/core/auth/types";
import { cn } from "@/lib/utils";

import { MobileNav, type MobileNavLink } from "./mobile-nav";

export type HeaderProps = {
  className?: string;
  homeURL?: string;
};

// 主导航 · 桌面端与移动端共用；点击锚点跳转本段
export const PRIMARY_LINKS: MobileNavLink[] = [
  { href: "#vision", label: "产品理念" },
  { href: "#industries", label: "行业落地" },
  { href: "#tech-stack", label: "技术架构" },
  { href: "#capabilities", label: "差异化能力" },
  { href: "#trust", label: "合规与背书" },
  { href: "#custom", label: "获取方案" },
];

export async function Header({ className, homeURL }: HeaderProps) {
  const isExternalHome = !homeURL;

  // 已登录访客直跳工作台；未登录访客跳登录（带回跳）
  const auth = await getServerSideUser();
  const ctaHref =
    auth.tag === "authenticated" ? "/workspace" : buildLoginUrl("/workspace");

  return (
    <header
      className={cn(
        "fixed top-4 right-4 left-4 z-50 mx-auto flex h-14 max-w-6xl items-center gap-4 rounded-2xl px-4 md:px-6",
        "neumo-raised",
        className,
      )}
    >
      {/* 左：品牌 */}
      <Link
        href={homeURL ?? "/"}
        target={isExternalHome ? "_blank" : "_self"}
        rel={isExternalHome ? "noopener noreferrer" : undefined}
        className="flex shrink-0 items-center gap-2 font-serif text-base font-semibold tracking-tight whitespace-nowrap text-[color:var(--neumo-text)]"
      >
        <OpieLogo className="size-6" />
        OPIE&nbsp;OS
      </Link>

      {/* 中：主导航（桌面端 ≥ sm 可见） */}
      <nav className="hidden flex-1 items-center justify-center gap-1 text-sm font-medium sm:flex">
        {PRIMARY_LINKS.map((link) => (
          <a
            key={link.href}
            href={link.href}
            className="rounded-lg px-3 py-1.5 text-[color:var(--neumo-text-muted)] transition-colors hover:bg-[color:var(--neumo-bg-soft)] hover:text-[color:var(--neumo-text)]"
          >
            {link.label}
          </a>
        ))}
      </nav>

      {/* 右：主题 + 立即体验（桌面端） */}
      <div className="ml-auto hidden items-center gap-2 sm:flex">
        <ThemeToggle className="neumo-btn size-9 rounded-xl p-0 text-[color:var(--neumo-text-muted)] hover:text-[color:var(--neumo-text)]" />
        <Link
          href={ctaHref}
          className="neumo-btn-accent rounded-xl px-4 py-2 text-xs font-medium"
        >
          立即体验
        </Link>
      </div>

      {/* 移动端菜单 */}
      <div className="ml-auto sm:hidden">
        <MobileNav links={PRIMARY_LINKS} ctaHref={ctaHref} />
      </div>
    </header>
  );
}
