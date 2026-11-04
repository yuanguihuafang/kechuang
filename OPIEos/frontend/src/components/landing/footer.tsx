import Link from "next/link";

import { OpieLogo } from "@/components/brand/opie-logo";
import { cn } from "@/lib/utils";

const COLUMNS = [
  {
    title: "产品",
    links: [
      { href: "#vision", label: "产品理念" },
      { href: "#tech-stack", label: "技术架构" },
      { href: "#execution-loop", label: "执行流程" },
      { href: "#capabilities", label: "差异化能力" },
      { href: "#trust", label: "部署与合规" },
      { href: "#custom", label: "获取方案" },
    ],
  },
  {
    title: "应用",
    links: [
      { href: "#industries", label: "行业落地" },
      { href: "/workspace", label: "工作台" },
      {
        href: "/showcase/21cfea46-34bd-4aa6-9e1f-3009452fbeb9",
        label: "演示会话",
      },
      { href: "/login", label: "登录" },
    ],
  },
  {
    title: "资源",
    links: [
      { label: "开发文档" },
      { label: "客户案例" },
      { label: "技能市场" },
      { label: "更新日志" },
    ],
  },
  {
    title: "团队",
    links: [
      { label: "关于我们" },
      { label: "联系方式" },
      { label: "招聘" },
      { label: "合作伙伴" },
    ],
  },
];

export type FooterProps = {
  className?: string;
};

export function Footer({ className }: FooterProps) {
  return (
    <footer
      className={cn(
        "neumo-surface relative mt-0 border-t border-[color:var(--neumo-border)]",
        className,
      )}
    >
      <div className="container mx-auto max-w-6xl px-4 py-16 md:px-6">
        <div className="grid grid-cols-2 gap-10 md:grid-cols-5">
          {/* 品牌列：logo + 标语 + 二次 CTA + 备案号 */}
          <div className="col-span-2 flex flex-col gap-4 md:col-span-1">
            <Link href="/" className="inline-flex items-center gap-2">
              <OpieLogo className="size-7" />
              <span className="font-serif text-base font-semibold tracking-tight">
                OPIE&nbsp;OS
              </span>
            </Link>
            <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
              企业级智能体操作系统——让每一条企业流程，都能被人工智能
              调用、确认与回写。
            </p>
            <Link
              href="/workspace"
              className="neumo-btn-accent mt-1 inline-flex w-fit items-center rounded-xl px-4 py-2 text-xs font-medium text-white"
            >
              立即体验 →
            </Link>
          </div>

          {/* 4 列链接 */}
          {COLUMNS.map((col) => (
            <div key={col.title}>
              <h4 className="mb-4 text-[12px] font-semibold tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
                {col.title}
              </h4>
              <ul className="space-y-2.5">
                {col.links.map((link) => (
                  <li key={link.label}>
                    {link.href ? (
                      <Link
                        href={link.href}
                        className="text-sm text-[color:var(--neumo-text-muted)] transition-colors hover:text-[color:var(--neumo-accent-text)]"
                      >
                        {link.label}
                      </Link>
                    ) : (
                      <span className="text-sm text-[color:var(--neumo-text-subtle)]">
                        {link.label}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        {/* 底部一行：版权 + 法律链接 + 备案号 */}
        <div className="mt-12 flex flex-col gap-3 border-t border-[color:var(--neumo-border)] pt-6 md:flex-row md:items-center md:justify-between">
          <p className="text-xs text-[color:var(--neumo-text-subtle)]">
            © {new Date().getFullYear()} OPIE 团队 · 让智能体成为企业的标准工种
          </p>
          <div className="flex flex-wrap items-center gap-4 text-xs text-[color:var(--neumo-text-subtle)]">
            <span>
              隐私条款
            </span>
            <span>
              服务条款
            </span>
            <span>网站数据政策</span>
            <span aria-hidden>·</span>
            <span>网站备案信息待补充</span>
          </div>
        </div>
      </div>
    </footer>
  );
}
