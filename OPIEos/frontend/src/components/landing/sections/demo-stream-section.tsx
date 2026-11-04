"use client";

import { CircleDotIcon, TerminalSquareIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

type LogLine = {
  text: string;
  kind: "cmd" | "out" | "ok" | "warn" | "info";
};

const DEMO_LINES: LogLine[] = [
  { kind: "cmd", text: "$ 让智能体分析近 30 天竞品动态并产出 PDF 报告" },
  { kind: "info", text: "▸ 识别任务目标 → 行业研究 + 结构化报告" },
  {
    kind: "info",
    text: "▸ 调度 3 个子智能体并行检索（公开数据 · 财报 · 社区）",
  },
  { kind: "out", text: "  · 子智能体 A 完成，吞吐 4.2 MB" },
  { kind: "out", text: "  · 子智能体 B 完成，吞吐 1.8 MB" },
  { kind: "warn", text: "  · 子智能体 C 命中限流，自动重试 1 次" },
  { kind: "ok", text: "✔ 检索完成，180 条原始证据已结构化" },
  { kind: "info", text: "▸ 上下文工程汇总，启动摘要压缩（节省 38% token）" },
  { kind: "info", text: "▸ 沙箱启动 LaTeX 容器，准备排版" },
  { kind: "cmd", text: "$ 智能体继续执行 → 生成大纲 → 填充章节 → 渲染 PDF" },
  {
    kind: "ok",
    text: "✔ 报告已生成：行业研究简报-2026Q3.pdf · 18 页 · 4.2 MB",
  },
  { kind: "info", text: "▸ 任务总耗时 4 分 12 秒 · 中间产物已全部留痕" },
];

type FacetTone = "blue" | "violet" | "amber" | "emerald";

const FACETS: {
  index: string;
  title: string;
  desc: string;
  tone: FacetTone;
}[] = [
  {
    index: "01",
    title: "自然语言即任务",
    desc: "用一句中文描述需求，智能体自动拆解目标与子任务。",
    tone: "blue",
  },
  {
    index: "02",
    title: "并行调度子智能体",
    desc: "检索、解析、生成三类工作并行推进，单任务最高提速 3 倍。",
    tone: "violet",
  },
  {
    index: "03",
    title: "沙箱内交付产物",
    desc: "报告、网页、幻灯片、数据可视化在隔离沙箱内完成，结束即交付。",
    tone: "amber",
  },
  {
    index: "04",
    title: "中间过程全留痕",
    desc: "每一步决策、检索来源、产物版本都有结构化日志，便于回溯。",
    tone: "emerald",
  },
];

const TONE_STYLES: Record<FacetTone, string> = {
  blue: "border-blue-200 bg-blue-50 text-blue-700",
  violet: "border-violet-200 bg-violet-50 text-violet-700",
  amber: "border-amber-200 bg-amber-50 text-amber-700",
  emerald: "border-emerald-200 bg-emerald-50 text-emerald-700",
};

const LINE_STYLES: Record<LogLine["kind"], string> = {
  cmd: "text-blue-700",
  info: "text-slate-500",
  out: "text-slate-600",
  ok: "text-emerald-700",
  warn: "text-amber-700",
};

export function DemoStreamSection({ className }: { className?: string }) {
  const [shown, setShown] = useState(0);

  useEffect(() => {
    if (shown >= DEMO_LINES.length) return;
    const t = setTimeout(() => setShown((n) => n + 1), 620);
    return () => clearTimeout(t);
  }, [shown]);

  return (
    <section
      className={cn("relative w-full bg-[#f5f7fa] py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="inline-flex w-fit items-center rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs font-medium tracking-wide text-amber-700 uppercase">
            实时执行
          </span>
          <h2 className="text-3xl font-bold tracking-tight text-slate-900 md:text-4xl">
            看一次任务
            <span className="text-blue-600">跑完的过程</span>
          </h2>
          <p className="max-w-2xl text-base leading-7 text-slate-600">
            下方的终端是一个真实任务的执行流：自然语言需求 → 拆解 → 调度子智能体
            → 沙箱渲染 → 交付产物。每一步都有结构化日志留痕，可回放、可审计。
          </p>
        </header>

        <div className="grid gap-6 md:grid-cols-[1.2fr_1fr] md:items-stretch">
          {/* 左侧终端 */}
          <div className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.04),0_8px_24px_rgba(15,23,42,0.04)]">
            <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50 px-4 py-2.5">
              <div className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-full bg-red-400" />
                <span className="h-2.5 w-2.5 rounded-full bg-amber-300" />
                <span className="h-2.5 w-2.5 rounded-full bg-emerald-400" />
              </div>
              <div className="flex items-center gap-1.5 text-[10px] tracking-widest text-slate-500 uppercase">
                <TerminalSquareIcon className="size-3" />
                task_8c4f21 · 流式
              </div>
              <div className="flex items-center gap-1 text-[10px] tracking-widest text-amber-700 uppercase">
                <CircleDotIcon className="size-3 animate-pulse" />
                运行中
              </div>
            </div>
            <pre className="m-0 overflow-x-auto bg-white px-5 py-4 font-mono text-[12.5px] leading-6 text-slate-700 md:min-h-[420px] md:text-sm md:leading-7">
              {DEMO_LINES.slice(0, shown).map((line, i) => (
                <span
                  key={i}
                  className={cn("block whitespace-pre", LINE_STYLES[line.kind])}
                >
                  {line.text}
                </span>
              ))}
              {shown < DEMO_LINES.length && (
                <span className="mt-1 inline-block h-4 w-2 animate-pulse bg-blue-600 align-middle" />
              )}
            </pre>
          </div>

          {/* 右侧：能力对照 */}
          <div className="flex flex-col gap-3">
            {FACETS.map((facet) => (
              <FacetCard key={facet.index} facet={facet} />
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

function FacetCard({ facet }: { facet: (typeof FACETS)[number] }) {
  return (
    <div className={cn("rounded-xl border p-4", TONE_STYLES[facet.tone])}>
      <div className="mb-2 flex items-center gap-2 text-xs font-medium tracking-widest uppercase">
        <span>{facet.index}</span>
        <span className="text-slate-300">/</span>
        <span className="text-slate-700">{facet.title}</span>
      </div>
      <p className="text-sm leading-6 text-slate-600">{facet.desc}</p>
    </div>
  );
}
