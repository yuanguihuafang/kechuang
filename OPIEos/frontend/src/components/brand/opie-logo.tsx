import { cn } from "@/lib/utils";

export type OpieLogoProps = {
  className?: string;
  size?: number;
};

/**
 * OPIE OS 品牌 Logo（终端光标标识）
 * 「>」命令提示符 + 光标块：命令行 / 智能体执行，呼应首页终端面板
 * 标准色：#2563EB（品牌蓝） + #7C3AED（点睛紫）
 */
export function OpieLogo({ className, size }: OpieLogoProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={cn("shrink-0", className)}
      aria-hidden="true"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M7 7 L14.5 12 L7 17"
        stroke="#2563EB"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <rect x="16" y="8" width="2.8" height="8" rx="1.2" fill="#7C3AED" />
    </svg>
  );
}
