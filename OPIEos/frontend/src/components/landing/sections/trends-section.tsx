import {
  TerminalIcon, // 01 系统 CLI 化
  WorkflowIcon, // 02 流程 Skill 化
  BotIcon, // 03 员工 Agent 化
  CheckCircle2Icon, // 可执行
  UsersIcon, // 可协同
  ShieldCheckIcon, // 可确认
  HistoryIcon, // 可留痕
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

type Trend = {
  id: string;
  index: string;
  title: string;
  subtitle: string; // 中文短标语
  desc: string; // 一句话定义
  proof: string; // 已印证案例（PPT 第 4 页原话）
  icon: LucideIcon;
};

const TRENDS: Trend[] = [
  {
    id: "cli",
    index: "01",
    title: "系统 CLI 化",
    subtitle: "在存量系统外加一层网关",
    desc: "把审批、查询、归档、通知等动作变成标准命令入口。存量系统底座不动，Agent 获得统一调用能力。",
    proof: "示例应用：ERP / OA / CRM / IoT 场景",
    icon: TerminalIcon,
  },
  {
    id: "skill",
    index: "02",
    title: "流程 Skill 化",
    subtitle: "把流程规则封装为可复用 Skill",
    desc: "合同、采购、审批、寻源等流程拆成可复用、可审计、可编排的 Skill——一整段规则，而非单次工具调用。",
    proof: "示例应用：寻源 / 知识库 / 审批 / 派单",
    icon: WorkflowIcon,
  },
  {
    id: "agent",
    index: "03",
    title: "员工 Agent 化",
    subtitle: "让数字员工替人执行",
    desc: "Agent 知道权限边界、理解常用流程，替员工发起查询与执行；遇到异常自动暂停、转人工。",
    proof: "示例应用：客服 / 销售 / 运营 / 编辑",
    icon: BotIcon,
  },
];

type Promise = {
  title: string; // 可执行 / 可协同 / 可确认 / 可留痕
  desc: string;
  icon: LucideIcon;
};

// 四可行动口号 —— PPT 第 5 页大字视觉焦点
const PROMISES: Promise[] = [
  {
    title: "可执行",
    desc: "不是聊一句就完，而是真的调用系统、跑完流程。",
    icon: CheckCircle2Icon,
  },
  {
    title: "可协同",
    desc: "跨系统、跨部门、跨岗位的协同由 Agent 主动编排。",
    icon: UsersIcon,
  },
  {
    title: "可确认",
    desc: "关键节点出确认卡，人核对了再落地，不擅自写业务系统。",
    icon: ShieldCheckIcon,
  },
  {
    title: "可留痕",
    desc: "每一步调用、确认、写入全程留痕，可被回看、被审计、被回放。",
    icon: HistoryIcon,
  },
];

export function TrendsSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="trends"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            三大趋势 · METHODOLOGY
          </span>
          <h2 className="font-serif text-3xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl">
            智能化阶段的
            <span className="text-[color:var(--neumo-accent-text)]">
              三个真正趋势
            </span>
          </h2>
          <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)]">
            不是所有企业都在追同一条路。我们观察到的智能化阶段，是这三条趋势同时到来——
            系统 CLI 化让存量系统可被调用，流程 Skill 化让规则可被复用，员工
            Agent 化让人机协 同有边界。
          </p>
        </header>

        {/* 三大趋势：大卡三列 */}
        <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
          {TRENDS.map((t) => (
            <TrendCard key={t.id} trend={t} />
          ))}
        </div>

        {/* 四可行动口号：大字 callout */}
        <div className="mt-12">
          <div className="neumo-raised rounded-2xl p-6 md:p-8">
            <div className="mb-6 flex items-center gap-3">
              <span className="font-mono text-[11px] font-semibold tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
                ▸ 四可
              </span>
              <span className="text-sm font-medium text-[color:var(--neumo-text)]">
                AgentOS 落到企业的四条硬承诺
              </span>
            </div>
            <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
              {PROMISES.map((p) => {
                const Icon = p.icon;
                return (
                  <div key={p.title} className="flex flex-col gap-2">
                    <div className="flex items-center gap-2">
                      <span className="neumo-inset-sm inline-flex size-9 items-center justify-center rounded-xl text-[color:var(--neumo-accent-text)]">
                        <Icon className="size-4" />
                      </span>
                      <h4 className="font-serif text-xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-2xl">
                        {p.title}
                      </h4>
                    </div>
                    <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
                      {p.desc}
                    </p>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function TrendCard({ trend }: { trend: Trend }) {
  const Icon = trend.icon;
  return (
    <article className="neumo-raised flex h-full flex-col gap-5 rounded-2xl p-7 md:p-8">
      <div className="flex items-center justify-between">
        <span className="neumo-inset-sm inline-flex size-12 items-center justify-center rounded-2xl text-[color:var(--neumo-accent-text)]">
          <Icon className="size-5" />
        </span>
        <span className="font-mono text-xs font-medium tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
          {trend.index}
        </span>
      </div>
      <div>
        <h3 className="text-xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-2xl">
          {trend.title}
        </h3>
        <p className="mt-1 text-sm font-medium text-[color:var(--neumo-accent-text)]">
          {trend.subtitle}
        </p>
      </div>
      <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
        {trend.desc}
      </p>
      <div className="mt-auto border-t border-dashed border-[color:var(--neumo-text-subtle)]/30 pt-4">
        <span className="text-[11px] font-medium tracking-wider text-[color:var(--neumo-text-subtle)] uppercase">
          {trend.proof}
        </span>
      </div>
    </article>
  );
}
