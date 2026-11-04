import {
  TargetIcon, // Now
  CompassIcon, // 1 年
  TelescopeIcon, // 3 年
} from "lucide-react";

import { cn } from "@/lib/utils";

type Milestone = {
  when: string; // 时间锚点
  icon: typeof TargetIcon;
  title: string;
  desc: string;
};

const MILESTONES: Milestone[] = [
  {
    when: "现在",
    icon: TargetIcon,
    title: "把底座跑通",
    desc: "把大模型、知识库、执行沙箱沉淀到同一个内核；让已落地的行业先跑起来。",
  },
  {
    when: "1 年内",
    icon: CompassIcon,
    title: "沉淀 100+ 可复用 Skill",
    desc: "让一支 Agent 团队可以在新行业 1 周内落地，方法论比功能更重要。",
  },
  {
    when: "3 年内",
    icon: TelescopeIcon,
    title: "成为企业 Agent 操作系统",
    desc: "类似数据库之于业务系统，让每一家公司都能像搭积木一样组合 Agent 团队。",
  },
];

export function VisionSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="vision"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            愿景与使命 · Mission
          </span>
          <h2 className="max-w-3xl font-serif text-3xl leading-[1.15] font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl lg:text-5xl">
            让 Agent 成为企业的
            <span className="text-[color:var(--neumo-accent-text)]">
              {" "}
              标准工种
            </span>
          </h2>
          <p className="max-w-3xl text-base leading-7 text-[color:var(--neumo-text-muted)] md:text-lg">
            OPIE OS 想做的事很简单：把 AI 从「聊一句话就完」的演示，落到企业里
            <span className="font-medium text-[color:var(--neumo-text)]">
              真正能交付的执行者
            </span>
            。我们相信，未来每一家公司都会有一支由 Agent 组成的数字员工团队——
            它们知道权限边界、理解常用流程、在关键节点交人确认、把结果写回原系统。
          </p>
        </header>

        {/* 时间锚点：现在 · 1 年 · 3 年 */}
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          {MILESTONES.map((m) => {
            const Icon = m.icon;
            return (
              <article
                key={m.when}
                className="neumo-raised flex h-full flex-col gap-4 rounded-2xl p-7"
              >
                <div className="flex items-center justify-between">
                  <span className="neumo-inset-sm inline-flex size-11 items-center justify-center rounded-2xl text-[color:var(--neumo-accent-text)]">
                    <Icon className="size-5" />
                  </span>
                  <span className="font-mono text-xs font-medium tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
                    {m.when}
                  </span>
                </div>
                <h3 className="text-lg font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-xl">
                  {m.title}
                </h3>
                <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
                  {m.desc}
                </p>
              </article>
            );
          })}
        </div>

        {/* 底部签名条：把品牌宣言写得更短 */}
        <div className="mt-12">
          <div className="neumo-inset-sm rounded-2xl px-6 py-6 text-center md:px-8 md:py-7">
            <p className="font-serif text-xl leading-snug font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-2xl">
              「好的 AI 不是会聊，是会交付。」
            </p>
            <p className="mt-3 text-sm text-[color:var(--neumo-text-muted)]">
              — OPIE OS 是为这一条标准而生的系统。
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}
