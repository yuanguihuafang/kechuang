"use client";

import {
  ArrowRightIcon,
  Link2Icon, // 链管家
  CompassIcon, // 工业寻源
  NewspaperIcon, // 广电
  SendIcon, // VELA
  SproutIcon, // 夏小肚
  NetworkIcon, // 产业大脑
  PlusIcon, // 占位卡
  TerminalSquareIcon,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

// 6 行业速览（用于 hero 右下）—— 不与 trends / capabilities / scenarios 重复，
// 只起视觉锚点作用：访客一眼看到「这产品已经在 6 个行业落地」
type IndustryMini = {
  id: string;
  name: string;
  en: string;
  kpi: string;
  color: string; // tailwind class (text/bg)
  icon: LucideIcon;
};

const INDUSTRY_MINIS: IndustryMini[] = [
  {
    id: "lg",
    name: "链管家",
    en: "L-Guardian",
    kpi: "1 天上手",
    color: "text-[#d85a30] bg-[#faece7]",
    icon: Link2Icon,
  },
  {
    id: "si",
    name: "工业寻源",
    en: "SourceIt",
    kpi: "30+ 数据源",
    color: "text-[#0f6e56] bg-[#e1f5ee]",
    icon: CompassIcon,
  },
  {
    id: "mc",
    name: "广电员工",
    en: "MediaCrew",
    kpi: "80% 替代",
    color: "text-[#185fa5] bg-[#e6f1fb]",
    icon: NewspaperIcon,
  },
  {
    id: "vl",
    name: "VELA",
    en: "AI 获客",
    kpi: "+192% 流量",
    color: "text-[#993556] bg-[#fbeaf0]",
    icon: SendIcon,
  },
  {
    id: "xl",
    name: "夏小肚",
    en: "XiaLab",
    kpi: "≤30s 问诊",
    color: "text-[#3b6d11] bg-[#eaf3de]",
    icon: SproutIcon,
  },
  {
    id: "co",
    name: "产业大脑",
    en: "ClusterOS",
    kpi: ">98% 撮合",
    color: "text-[#854f0b] bg-[#faeeda]",
    icon: NetworkIcon,
  },
];

// 占位卡片——代表「还有更多行业可用此底座」，避免用户觉得只有这 6 个

// 终端脚本：换为「链管家 · 拉链 ERP 录单」企业向演示，与 6 行业呼应
const TERMINAL_SCRIPT: { kind: TerminalKind; text: string }[] = [
  { kind: "prompt", text: "$ opie.os" },
  { kind: "arg", text: "  --industry=" },
  { kind: "value", text: '"拉链 ERP"' },
  { kind: "arg", text: "  --skill=" },
  { kind: "value", text: '"链管家 · 录单确认"' },
  { kind: "log", text: "▸ 加载行业知识 · 拉链色卡 / 工艺字段已就绪" },
  { kind: "log", text: "▸ 连接存量 ERP · 5 张表回写权限已授权" },
  { kind: "log", text: "▸ 工作流编排 · Card → ERP API → 复核" },
  { kind: "ok", text: "✔ 新员工录单 · 准确率 99.2% · 时长 -60%" },
];

type TerminalKind = "prompt" | "arg" | "value" | "log" | "ok";
type TerminalToken = { kind: TerminalKind; text: string };

const TOKEN_STYLES: Record<TerminalKind, string> = {
  prompt: "text-[color:var(--neumo-accent-text)]",
  arg: "text-[color:var(--neumo-text-muted)]",
  value: "text-[color:var(--neumo-info)]",
  log: "text-[color:var(--neumo-text-muted)]",
  ok: "text-[color:var(--neumo-success)]",
};

export function Hero({ className }: { className?: string }) {
  return (
    <section
      className={cn(
        "neumo-surface relative isolate w-full overflow-hidden pt-32 pb-20 md:pt-36 md:pb-24",
        className,
      )}
    >
      <HeroGridBackdrop />

      <div className="relative z-10 container mx-auto max-w-6xl px-4 md:px-6">
        <div className="grid items-start gap-10 md:grid-cols-2 md:gap-16">
          {/* 左：状态、标题、CTA */}
          <div className="flex flex-col">
            <div className="mb-6 flex flex-wrap items-center gap-3">
              <span className="neumo-inset-sm inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-medium text-[color:var(--neumo-success)]">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[color:var(--neumo-success)] opacity-60" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-[color:var(--neumo-success)]" />
                </span>
                系统在线
              </span>
              <span className="neumo-inset-sm rounded-full px-3 py-1.5 text-xs font-medium text-[color:var(--neumo-accent-text)]">
                通用 · 已落地多个行业
              </span>
            </div>

            <p className="mb-4 text-xs font-medium tracking-[0.18em] text-[color:var(--neumo-text-subtle)] uppercase">
              企业级 Agent 操作系统 · ENTERPRISE-GRADE AGENT OS
            </p>

            <h1 className="font-serif text-4xl leading-[1.08] font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-5xl lg:text-[56px] lg:leading-[1.06]">
              <span>让 Agent 成为企业的</span>{" "}
              <span className="text-[color:var(--neumo-accent-text)]">
                标准工种
              </span>
            </h1>

            <p className="mt-3 text-lg font-medium tracking-tight text-[color:var(--neumo-text)] md:text-xl">
              让每一条企业流程，都能被 AI
              <span className="text-[color:var(--neumo-accent-text)]">
                {" "}
                调用、确认与回写。
              </span>
            </p>

            <p className="mt-5 max-w-xl text-base leading-7 text-[color:var(--neumo-text-muted)] md:text-lg">
              企业不是缺系统，而是缺一个把存量系统、流程规则、岗位动作串起来的
              AI 办事入口。 OPIE OS 是通用的 Agent
              操作系统——把大模型、知识库、工具链、执行沙箱沉淀到同一个内核， 让
              AI
              在企业里真正能交付。下面是几个已落地的行业示例，方法论可复用到任意行业。
            </p>

            <div className="mt-8 flex flex-col gap-3 sm:flex-row">
              <Link
                href="/workspace"
                className="neumo-btn-accent group inline-flex h-12 items-center justify-center gap-2 rounded-2xl px-6 text-sm font-medium text-white"
              >
                立即体验
                <ArrowRightIcon className="size-4 transition-transform group-hover:translate-x-0.5" />
              </Link>
              <Link
                href="#industries"
                className="neumo-btn inline-flex h-12 items-center justify-center rounded-2xl px-6 text-sm font-medium text-[color:var(--neumo-text)]"
              >
                查看行业落地 →
              </Link>
            </div>
          </div>

          {/* 右：终端 + 6 行业速览 */}
          <div className="flex flex-col gap-6">
            <TerminalPanel />
            <IndustriesMiniMatrix />
          </div>
        </div>
      </div>
    </section>
  );
}

function TerminalPanel() {
  const [shown, setShown] = useState<TerminalToken[]>([]);
  const [typed, setTyped] = useState(0);

  useEffect(() => {
    if (shown.length >= TERMINAL_SCRIPT.length) return;
    const t = setTimeout(() => {
      const next = TERMINAL_SCRIPT[shown.length]!;
      setShown((prev) => [...prev, next]);
      setTyped(0);
    }, 380);
    return () => clearTimeout(t);
  }, [shown]);

  useEffect(() => {
    const current = TERMINAL_SCRIPT[shown.length];
    if (!current) return;
    if (current.kind === "log" || current.kind === "ok") {
      setTyped(current.text.length);
      return;
    }
    if (typed < current.text.length) {
      const t = setTimeout(() => setTyped((n) => n + 1), 24);
      return () => clearTimeout(t);
    }
  }, [shown, typed]);

  return (
    <div className="neumo-raised overflow-hidden rounded-2xl">
      <div className="flex items-center justify-between border-b border-[color:var(--neumo-border)] px-4 py-2.5">
        <div className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-full bg-[color:var(--neumo-danger)]" />
          <span className="size-2.5 rounded-full bg-[color:var(--neumo-warning)]" />
          <span className="size-2.5 rounded-full bg-[color:var(--neumo-success)]" />
        </div>
        <div className="flex items-center gap-1.5 text-[10px] tracking-widest text-[color:var(--neumo-text-muted)] uppercase">
          <TerminalSquareIcon className="size-3" />
          opie-cli · 演示：链管家场景
        </div>
        <div className="flex items-center gap-1 text-[10px] tracking-widest text-[color:var(--neumo-success)] uppercase">
          <span className="size-2 rounded-full bg-[color:var(--neumo-success)]" />
          已连接
        </div>
      </div>
      <pre className="neumo-inset m-3 overflow-x-auto rounded-xl px-5 py-4 font-mono text-[12.5px] leading-6 text-[color:var(--neumo-text)] md:text-[13px] md:leading-7">
        {shown.map((token, i) => {
          const isLast = i === shown.length - 1;
          const text =
            isLast && token.kind !== "log" && token.kind !== "ok"
              ? token.text.slice(0, typed)
              : token.text;
          return (
            <span
              key={i}
              className={cn("block whitespace-pre", TOKEN_STYLES[token.kind])}
            >
              {text}
              {isLast && token.kind !== "log" && token.kind !== "ok" && (
                <span className="-mb-0.5 ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-[color:var(--neumo-accent)] align-middle" />
              )}
            </span>
          );
        })}
      </pre>
    </div>
  );
}

function IndustriesMiniMatrix() {
  return (
    <div className="neumo-raised rounded-2xl p-2">
      <div className="neumo-inset-sm flex items-center justify-between rounded-xl px-5 py-2.5">
        <p className="text-xs font-medium tracking-wide text-[color:var(--neumo-text-muted)] uppercase">
          行业落地示例
        </p>
        <Link
          href="#industries"
          className="text-[11px] font-medium tracking-wide text-[color:var(--neumo-accent-text)]"
        >
          查看全部 →
        </Link>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-1.5 px-1 pb-1">
        {INDUSTRY_MINIS.map((item) => {
          const Icon = item.icon;
          return (
            <Link
              key={item.id}
              href="#industries"
              className="group flex items-center gap-2.5 rounded-xl px-3 py-2.5 transition-colors hover:bg-[color:var(--neumo-bg-soft)]"
            >
              <span
                className={cn(
                  "neumo-inset-sm inline-flex size-9 shrink-0 items-center justify-center rounded-xl",
                  item.color,
                )}
              >
                <Icon className="size-4" />
              </span>
              <div className="min-w-0">
                <p className="text-sm font-semibold text-[color:var(--neumo-text)]">
                  {item.name}
                </p>
                <p className="mt-0.5 text-[11px] leading-4 text-[color:var(--neumo-text-muted)]">
                  {item.kpi}
                </p>
              </div>
            </Link>
          );
        })}
        {/* 占位卡：暗示「还有更多行业可用此底座」 */}
        <div className="flex items-center gap-2.5 rounded-xl border border-dashed border-[color:var(--neumo-border)] px-3 py-2.5">
          <span className="neumo-inset-sm inline-flex size-9 shrink-0 items-center justify-center rounded-xl text-[color:var(--neumo-accent-text)]">
            <PlusIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-[color:var(--neumo-text)]">
              其他行业
            </p>
            <p className="mt-0.5 text-[11px] leading-4 text-[color:var(--neumo-text-muted)]">
              用同套底座扩展
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

function HeroGridBackdrop() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 -z-10 opacity-60"
      style={{
        background:
          "radial-gradient(ellipse 70% 50% at 30% 20%, rgba(255,255,255,0.6) 0%, rgba(255,255,255,0) 60%)",
      }}
    />
  );
}
