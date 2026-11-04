import {
  CpuIcon, // 01 多模型路由
  PlugZapIcon, // 02 跨系统接入
  BlocksIcon, // 03 Skill 即资产
  ShieldCheckIcon, // 04 确认式写入
  ScaleIcon, // 05 企业级治理
  ServerIcon, // 06 私有化部署
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

type Capability = {
  title: string;
  desc: string;
  icon: LucideIcon;
  span: string;
  tag: string;
  highlight?: string;
};

// 6 项差异化亮点：避开「执行流程」叙述，专讲「OPIE OS 比通用 Agent 强在哪」
// 不与 architecture-section 的"5 步执行闭环 + 3 条贯穿机制"重复
const CAPABILITIES: Capability[] = [
  {
    title: "多模型路由器",
    desc: "不绑定单一 LLM。推理 / 视觉 / 长上下文 / 行业模型按任务自动路由；用户也可锁定指定模型。",
    icon: CpuIcon,
    span: "md:col-span-2 md:row-span-2",
    tag: "01 / 多模型",
    highlight: "按任务挑模型 · 不被供应商锁死",
  },
  {
    title: "跨系统接入",
    desc: "MCP 把现有 ERP / CRM / OA / IoT 一句话纳入统一入口，不动存量系统底座。",
    icon: PlugZapIcon,
    span: "md:col-span-2",
    tag: "02 / 系统集成",
  },
  {
    title: "Skill 即资产",
    desc: "流程规则封装为可复用的 Skill，可跨行业移植、可跨租户共享。一次写好，长期受益。",
    icon: BlocksIcon,
    span: "",
    tag: "03 / 方法论",
    highlight: "Skill 是产品形态 · 不是 prompt",
  },
  {
    title: "人核后再写",
    desc: "任何写业务系统的动作都先出确认卡——人核对了再落地，杜绝自动错单。",
    icon: ShieldCheckIcon,
    span: "",
    tag: "04 / 写入策略",
  },
  {
    title: "企业级治理",
    desc: "调用统计 / 日志追踪 / 数据脱敏 / 合规审计，按岗位授权、越权即止，全程可追溯。",
    icon: ScaleIcon,
    span: "",
    tag: "05 / 治理",
  },
  {
    title: "私有化部署",
    desc: "整套可私有化交付给客户。数据不出域，适合金融、政企、有数据主权要求的企业。",
    icon: ServerIcon,
    span: "",
    tag: "06 / 部署",
  },
];

export function CapabilityBentoSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="capabilities"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            Why OPIE OS · 不是又一个聊天机器人
          </span>
          <h2 className="font-serif text-3xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl">
            六项差异化，
            <span className="text-[color:var(--neumo-accent-text)]">
              把 Agent 用到企业现场
            </span>
          </h2>
          <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)]">
            通用聊天工具答一句话就完了；OPIE OS 把多模型、跨系统、Skill
            化、确认写入、治理与私有化 收进同一个内核——让你在企业里跑
            Agent，真正能交付，而不是只能 demo。
          </p>
        </header>

        {/* bento 6 卡 · 沿用原 auto-rows 与 4 列结构 */}
        <div className="grid auto-rows-[180px] grid-cols-1 gap-5 md:grid-cols-4 md:gap-6">
          {CAPABILITIES.map((c) => (
            <CapabilityCard key={c.title} capability={c} />
          ))}
        </div>
      </div>
    </section>
  );
}

function CapabilityCard({ capability }: { capability: Capability }) {
  const Icon = capability.icon;
  const isLarge = capability.span.includes("row-span-2");
  return (
    <div
      className={cn(
        "neumo-raised group relative flex flex-col justify-between overflow-hidden rounded-2xl p-6 transition-all duration-300 ease-in-out md:p-7",
        capability.span,
      )}
    >
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10px] tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
          {capability.tag}
        </div>
        <div className="neumo-inset-sm inline-flex size-9 items-center justify-center rounded-xl text-[color:var(--neumo-accent-text)]">
          <Icon className="size-4" />
        </div>
      </div>
      <div className="relative">
        <h3
          className={cn(
            "mb-2 font-semibold tracking-tight text-[color:var(--neumo-text)]",
            isLarge ? "text-2xl md:text-3xl" : "text-lg md:text-xl",
          )}
        >
          {capability.title}
        </h3>
        <p className="max-w-md text-sm leading-6 text-[color:var(--neumo-text-muted)]">
          {capability.desc}
        </p>
      </div>
      {capability.highlight && (
        <div className="neumo-inset-sm mt-2 inline-flex w-fit items-center gap-1 rounded-full px-3 py-1 font-mono text-[10px] tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
          ▸ {capability.highlight}
        </div>
      )}
    </div>
  );
}
