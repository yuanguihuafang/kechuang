import {
  CompassIcon, // 1 意图识别
  GitBranchPlusIcon, // 2 路由分发
  BlocksIcon, // 3 调用 Skill
  WorkflowIcon, // 4 Workflow 执行
  LayoutTemplateIcon, // 5 结果呈现
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

type Step = {
  id: string;
  index: string;
  title: string;
  subtitle: string;
  desc: string;
  icon: LucideIcon;
  accent: string; // 用现有 neumo 变量
};

const STEPS: Step[] = [
  {
    id: "intent",
    index: "01",
    title: "意图识别与任务拆解",
    subtitle: "UNDERSTAND",
    desc: "Host Agent 理解自然语言，明确目标与约束，生成可执行的计划与边界。",
    icon: CompassIcon,
    accent: "var(--neumo-accent)",
  },
  {
    id: "route",
    index: "02",
    title: "路由分发",
    subtitle: "ROUTE",
    desc: "从业务 Agent 池中匹配最合适的执行者，把子任务分发给对的数字员工。",
    icon: GitBranchPlusIcon,
    accent: "var(--neumo-info)",
  },
  {
    id: "skill",
    index: "03",
    title: "调用 Skill",
    subtitle: "CALL SKILL",
    desc: "Agent 按任务从 Skill 库调用对应技能，组合多 Skill 完成复杂编排。",
    icon: BlocksIcon,
    accent: "var(--neumo-success)",
  },
  {
    id: "workflow",
    index: "04",
    title: "Workflow 执行",
    subtitle: "EXECUTE",
    desc: "标准化工作流：Start → Card → API → End。每一个节点都可被审、被改、被回退。",
    icon: WorkflowIcon,
    accent: "var(--neumo-warning)",
  },
  {
    id: "result",
    index: "05",
    title: "结果呈现",
    subtitle: "DELIVER",
    desc: "执行结果以节点卡片动态呈现，全程留痕、可追溯，可被追问与回看。",
    icon: LayoutTemplateIcon,
    accent: "var(--neumo-accent)",
  },
];

// 贯穿整条流水线的「人在关键节点确认」机制，从 5 步扩成独立的小条
const MECHANISMS = [
  {
    title: "可交互卡片",
    desc: "数据、结论、建议以卡片呈现，可点、可确认、可追问。",
  },
  {
    title: "确认式写入",
    desc: "写业务系统前先出确认卡，人工核对后再落地，杜绝自动错单。",
  },
  {
    title: "异常即暂停",
    desc: "缺字段 / 超权限 / 规则冲突，自动暂停并转人工接手。",
  },
];

export function ExecutionLoopSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="execution-loop"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            <LayoutTemplateIcon className="size-3" />
            系统架构 · Execution Loop
          </span>
          <h2 className="font-serif text-3xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl">
            五步闭环，
            <span className="text-[color:var(--neumo-accent-text)]">
              从一句话到结果卡片
            </span>
          </h2>
          <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)]">
            从自然语言意图出发，经过路由分发、Skill 调用、Workflow 执行，
            最终以节点卡片的结果回到用户眼前——企业不是缺系统，而是缺一条把系统、流程、岗位动作串起来的
            AI 办事入口。
          </p>
        </header>

        {/* 五步流程：横向 bento，大卡描主轴 */}
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-5">
          {STEPS.map((step) => (
            <StepCard key={step.id} step={step} />
          ))}
        </div>

        {/* 贯穿机制：节点卡片 + 人在关键节点确认 */}
        <div className="mt-10">
          <div className="neumo-raised rounded-2xl p-6 md:p-7">
            <div className="mb-4 flex items-center gap-2">
              <span className="font-mono text-[11px] font-semibold tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
                ▸ 贯穿机制
              </span>
              <span className="text-sm font-medium text-[color:var(--neumo-text)]">
                节点卡片 + 人在关键节点确认
              </span>
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
              {MECHANISMS.map((m) => (
                <div
                  key={m.title}
                  className="neumo-inset-sm rounded-xl px-4 py-3"
                >
                  <div className="text-sm font-semibold text-[color:var(--neumo-text)]">
                    {m.title}
                  </div>
                  <p className="mt-1 text-sm leading-6 text-[color:var(--neumo-text-muted)]">
                    {m.desc}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function StepCard({ step }: { step: Step }) {
  const Icon = step.icon;
  return (
    <div className="neumo-raised flex h-full flex-col rounded-2xl p-6">
      <div className="mb-5 flex items-center justify-between">
        <span
          className="neumo-inset-sm inline-flex size-10 items-center justify-center rounded-xl"
          style={{ color: step.accent }}
        >
          <Icon className="size-4" />
        </span>
        <span
          className="font-mono text-xs font-medium tracking-widest uppercase"
          style={{ color: step.accent }}
        >
          {step.index}
        </span>
      </div>
      <h3 className="text-base font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-lg">
        {step.title}
      </h3>
      <p className="mt-1 text-[10px] tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
        {step.subtitle}
      </p>
      <p className="mt-4 text-sm leading-6 text-[color:var(--neumo-text-muted)]">
        {step.desc}
      </p>
    </div>
  );
}
