import {
  SparklesIcon, // 定制业务
  PlugZapIcon, // 对接存量系统
  ShieldCheckIcon, // 合规与部署
  ArrowRightIcon,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";

const VALUE_CARDS: { title: string; desc: string; icon: LucideIcon }[] = [
  {
    title: "按你的业务定制",
    desc: "把你企业的流程规则沉淀为专属 Skill 库，方法论复用，规则由你定。",
    icon: SparklesIcon,
  },
  {
    title: "对接你已有的系统",
    desc: "现有 ERP / CRM / OA / IM / IoT 都能纳进统一入口，存量系统不需要推倒。",
    icon: PlugZapIcon,
  },
  {
    title: "满足你的合规与数据要求",
    desc: "私有化部署、按岗位授权、审计追溯。等保 / ISO / 国家级双跨资质完整。",
    icon: ShieldCheckIcon,
  },
];

const STEPS = [
  "30 分钟咨询 · 梳理业务与流程",
  "联合共创 · 沉淀专属 Skill 库",
  "灰度上线 · 流程 Skill 化跑通",
  "陪跑迭代 · 让 Agent 越用越熟",
];

export function EnterpriseCtaSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="custom"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        {/* 主 CTA 卡片：浅紫强调色，呼应 OPIE 主品牌 */}
        <div className="neumo-raised relative overflow-hidden rounded-3xl border border-[color:var(--neumo-border)] bg-[color:var(--neumo-bg-soft)] p-8 md:p-12">
          <div className="relative z-10 flex flex-col gap-10">
            {/* 顶部：tag + slogan + 描述 */}
            <header className="flex flex-col items-center gap-5 text-center">
              <span className="neumo-inset-sm inline-flex items-center rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
                专属定制 · ENTERPRISE
              </span>
              <h2 className="max-w-3xl font-serif text-3xl leading-[1.15] font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl lg:text-5xl">
                给您的企业，
                <span className="text-[color:var(--neumo-accent-text)]">
                  做一套专属的 Agent OS
                </span>
              </h2>
              <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)] md:text-lg">
                OPIE OS 的通用底座可按你的业务深度定制——
                <span className="font-medium text-[color:var(--neumo-text)]">
                  不是给你一个 chat 工具，是按你的流程定做一套 AI 办事入口。
                </span>
                下面是我们会陪你走的四步。
              </p>
            </header>

            {/* 3 张定制价值卡 */}
            <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
              {VALUE_CARDS.map((card) => {
                const Icon = card.icon;
                return (
                  <article
                    key={card.title}
                    className="neumo-inset flex flex-col gap-3 rounded-2xl p-6"
                  >
                    <span className="neumo-raised inline-flex size-10 w-fit items-center justify-center rounded-xl text-[color:var(--neumo-accent-text)]">
                      <Icon className="size-5" />
                    </span>
                    <h3 className="text-base font-semibold text-[color:var(--neumo-text)] md:text-lg">
                      {card.title}
                    </h3>
                    <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
                      {card.desc}
                    </p>
                  </article>
                );
              })}
            </div>

            {/* 4 步合作流程 */}
            <div>
              <div className="mb-4 flex items-center justify-center gap-2">
                <span className="font-mono text-[11px] font-semibold tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
                  ▸ 合作流程
                </span>
              </div>
              <ol className="grid grid-cols-1 gap-3 md:grid-cols-4">
                {STEPS.map((step, i) => (
                  <li
                    key={step}
                    className="neumo-raised flex items-center gap-3 rounded-xl px-4 py-3"
                  >
                    <span className="neumo-inset-sm inline-flex size-7 shrink-0 items-center justify-center rounded-lg font-mono text-[12px] font-semibold text-[color:var(--neumo-accent-text)]">
                      0{i + 1}
                    </span>
                    <span className="text-sm font-medium text-[color:var(--neumo-text)]">
                      {step}
                    </span>
                  </li>
                ))}
              </ol>
            </div>

            {/* CTA 按钮组 */}
            <div className="flex flex-col items-center gap-3 pt-2 sm:flex-row sm:justify-center">
              <Link
                href="/workspace"
                className="neumo-btn-accent inline-flex h-12 items-center justify-center gap-2 rounded-2xl px-7 text-sm font-medium text-white"
              >
                预约 30 分钟咨询
                <ArrowRightIcon className="size-4" />
              </Link>
              <Link
                href="#industries"
                className="neumo-btn inline-flex h-12 items-center justify-center rounded-2xl px-7 text-sm font-medium text-[color:var(--neumo-text)]"
              >
                查看行业示例 →
              </Link>
            </div>

            {/* 底部签名条 */}
            <p className="text-center text-xs tracking-wider text-[color:var(--neumo-text-subtle)] uppercase">
              80 万 + 企业正在用 OPIE OS 的方法论。让 Agent
              成为你的下一个标准工种。
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
