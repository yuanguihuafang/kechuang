import {
  ShieldCheckIcon, // 安全合规
  ServerIcon, // 部署形态
  CheckCircle2Icon, // 子项 check
  CloudIcon, // SaaS 部署
  CombineIcon, // 混合云
  type LucideIcon,
} from "lucide-react";

import { cn } from "@/lib/utils";

type BigStat = {
  value: string;
  unit: string;
  label: string;
  caption: string;
};

const STATS: BigStat[] = [
  {
    value: "80",
    unit: "万 +",
    label: "平台注册企业",
    caption: "持续为中小企业提供 AI 办事入口",
  },
  {
    value: "22",
    unit: "个",
    label: "已覆盖行业",
    caption: "机械 / 家电 / 电子 / 轻工 等 9 大领域",
  },
  {
    value: "20",
    unit: "+ 省市",
    label: "服务区域",
    caption: "区域化赋能 · 集群链式转型",
  },
  {
    value: "23",
    unit: "位",
    label: "国家级双跨评价",
    caption: "2025 排名再进 6 位",
  },
];

type Deployment = {
  title: string;
  desc: string;
  icon: LucideIcon;
  tag: string; // 推荐 / 适用
};

const DEPLOYMENTS: Deployment[] = [
  {
    title: "公有云 SaaS",
    desc: "上手最快，按需扩缩容。适合中小客户快速验证价值。",
    icon: CloudIcon,
    tag: "推荐 · 中小客户",
  },
  {
    title: "私有化部署",
    desc: "整套可私有化交付给客户，数据不出域，金融与政企首选。",
    icon: ServerIcon,
    tag: "数据主权",
  },
  {
    title: "混合云",
    desc: "敏感数据在私有域、算力在公有云，跨域统一调度。",
    icon: CombineIcon,
    tag: "可平衡",
  },
];

const CERTS = [
  { label: "国家级双跨平台", note: "工信部 2023-2025 连续入选" },
  {
    label: "国家级中小企业公共服务示范平台",
    note: "特色专业型 + 服务型制造示范",
  },
  { label: "工信部链式数字化典型案例", note: "联合中国银行授信超 10 亿" },
  { label: "等保三级 · ISO 27001", note: "完整合规与审计体系" },
];

export function TrustSection({ className }: { className?: string }) {
  return (
    <section
      data-reveal
      id="trust"
      className={cn("neumo-surface relative w-full py-20 md:py-24", className)}
    >
      <div className="container mx-auto max-w-6xl px-4 md:px-6">
        <header className="mb-12 flex flex-col items-start gap-3 md:mb-16">
          <span className="neumo-inset-sm inline-flex w-fit items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-medium tracking-wider text-[color:var(--neumo-accent-text)] uppercase">
            <ShieldCheckIcon className="size-3" />
            信任与背书 · TRUST
          </span>
          <h2 className="font-serif text-3xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-4xl">
            不是又一家 AI 创业公司——
            <span className="text-[color:var(--neumo-accent-text)]">
              一个有国家级背书的底座
            </span>
          </h2>
          <p className="max-w-2xl text-base leading-7 text-[color:var(--neumo-text-muted)]">
            国家级双跨平台 · 国家中小企业公共服务示范平台 ·
            工信部链式数字化典型案例 ——下面是具体的数字、部署形态与合规清单。
          </p>
        </header>

        {/* 数字大屏：一行 4 个 */}
        <div className="neumo-raised rounded-2xl p-6 md:p-8">
          <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
            {STATS.map((s) => (
              <div key={s.label} className="flex flex-col gap-1.5">
                <div className="flex items-baseline gap-1">
                  <span className="font-serif text-4xl font-semibold tracking-tight text-[color:var(--neumo-text)] md:text-5xl">
                    {s.value}
                  </span>
                  <span className="text-base font-medium text-[color:var(--neumo-accent-text)] md:text-lg">
                    {s.unit}
                  </span>
                </div>
                <div className="text-sm font-semibold text-[color:var(--neumo-text)]">
                  {s.label}
                </div>
                <div className="text-[11px] leading-4 text-[color:var(--neumo-text-muted)]">
                  {s.caption}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* 部署形态：3 列 */}
        <div className="mt-10">
          <div className="mb-4 flex items-center gap-2">
            <span className="font-mono text-[11px] font-semibold tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
              ▸ 部署形态 · Deployment
            </span>
          </div>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
            {DEPLOYMENTS.map((d) => {
              const Icon = d.icon;
              return (
                <article
                  key={d.title}
                  className="neumo-raised flex h-full flex-col gap-3 rounded-2xl p-6"
                >
                  <div className="flex items-center justify-between">
                    <span className="neumo-inset-sm inline-flex size-10 items-center justify-center rounded-xl text-[color:var(--neumo-accent-text)]">
                      <Icon className="size-4" />
                    </span>
                    <span className="neumo-inset-sm inline-flex items-center rounded-full px-2.5 py-1 text-[10.5px] font-medium tracking-wider text-[color:var(--neumo-text-muted)] uppercase">
                      {d.tag}
                    </span>
                  </div>
                  <h3 className="text-lg font-semibold text-[color:var(--neumo-text)]">
                    {d.title}
                  </h3>
                  <p className="text-sm leading-6 text-[color:var(--neumo-text-muted)]">
                    {d.desc}
                  </p>
                </article>
              );
            })}
          </div>
        </div>

        {/* 合规徽章：4 行 */}
        <div className="mt-10">
          <div className="mb-4 flex items-center gap-2">
            <span className="font-mono text-[11px] font-semibold tracking-widest text-[color:var(--neumo-accent-text)] uppercase">
              ▸ 合规与资质 · Certifications
            </span>
          </div>
          <ul className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {CERTS.map((c) => (
              <li
                key={c.label}
                className="neumo-inset-sm flex items-start gap-3 rounded-xl px-4 py-3"
              >
                <CheckCircle2Icon className="mt-0.5 size-5 shrink-0 text-[color:var(--neumo-success)]" />
                <div>
                  <div className="text-sm font-semibold text-[color:var(--neumo-text)]">
                    {c.label}
                  </div>
                  <div className="mt-0.5 text-xs leading-5 text-[color:var(--neumo-text-muted)]">
                    {c.note}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
