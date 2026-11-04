"use client";

import {
  CheckIcon,
  ClipboardIcon,
  Clock3Icon,
  Link2Icon,
  MailPlusIcon,
  ShieldCheckIcon,
  UserRoundPlusIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { fetch as fetchWithAuth, getCsrfHeaders } from "@/core/api/fetcher";

type Invitation = {
  id: string;
  email: string;
  status: string;
  expires_at: string;
  created_at?: string;
  invite_url?: string;
};

function statusLabel(status: string) {
  if (status === "used")
    return { label: "已使用", color: "text-[var(--neumo-success)]" };
  if (status === "revoked")
    return { label: "已撤销", color: "text-[var(--neumo-danger)]" };
  return { label: "待使用", color: "text-[var(--neumo-accent-text)]" };
}

export default function InvitationsPage() {
  const [email, setEmail] = useState("");
  const [items, setItems] = useState<Invitation[]>([]);
  const [created, setCreated] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    const response = await fetchWithAuth("/api/v1/auth/admin/invitations");
    if (response.ok) setItems(await response.json());
  };
  useEffect(() => {
    void load();
  }, []);

  const create = async () => {
    setError(null);
    setCopied(false);
    setLoading(true);
    try {
      const response = await fetchWithAuth("/api/v1/auth/admin/invitations", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getCsrfHeaders() },
        body: JSON.stringify({ email }),
      });
      const body = (await response.json().catch(() => null)) as
        | (Invitation & { detail?: string })
        | null;
      if (!response.ok) {
        if (response.status === 403 && body?.detail?.includes("CSRF")) {
          window.location.href = `/login?next=${encodeURIComponent("/workspace/admin/invitations")}`;
          return;
        }
        setError(body?.detail ?? "创建邀请失败，请重试");
        return;
      }
      setCreated(`${window.location.origin}${body?.invite_url ?? ""}`);
      setEmail("");
      await load();
    } finally {
      setLoading(false);
    }
  };

  const copyLink = async () => {
    if (!created) return;
    await navigator.clipboard.writeText(created);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  };

  return (
    <main className="min-h-full overflow-y-auto px-4 py-8 text-[var(--neumo-text)] sm:px-8 lg:px-12">
      <div className="mx-auto max-w-5xl space-y-8">
        <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <div className="mb-3 flex items-center gap-2 text-xs font-medium tracking-[0.18em] text-[var(--neumo-accent-text)] uppercase">
              <ShieldCheckIcon className="size-4" />
              管理员中心
            </div>
            <h1 className="text-3xl font-semibold tracking-tight">用户邀请</h1>
            <p className="mt-2 max-w-xl text-sm leading-6 text-[var(--neumo-text-muted)]">
              邀请可信用户加入 OPIE OS。每个邀请链接有效 72 小时，只能使用一次。
            </p>
          </div>
          <div className="neumo-inset-sm flex items-center gap-2 rounded-2xl px-3 py-2 text-xs text-[var(--neumo-text-muted)]">
            <Clock3Icon className="size-4 text-[var(--neumo-accent-text)]" />
            单次使用 · 72 小时有效
          </div>
        </header>

        <section className="neumo-raised overflow-hidden rounded-3xl p-1">
          <div className="neumo-inset-sm rounded-[1.4rem] p-5 sm:p-7">
            <div className="flex items-start gap-4">
              <div className="neumo-raised-sm flex size-11 shrink-0 items-center justify-center rounded-2xl text-[var(--neumo-accent-text)]">
                <UserRoundPlusIcon className="size-5" />
              </div>
              <div>
                <h2 className="font-semibold">创建新邀请</h2>
                <p className="mt-1 text-sm text-[var(--neumo-text-muted)]">
                  输入用户邮箱，生成一条可复制的专属邀请链接。
                </p>
              </div>
            </div>
            <div className="mt-6 flex flex-col gap-3 sm:flex-row">
              <div className="relative flex-1">
                <MailPlusIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-[var(--neumo-text-subtle)]" />
                <Input
                  className="h-11 pl-10"
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="user@company.com"
                  aria-label="用户邮箱"
                />
              </div>
              <Button
                variant="accent"
                size="lg"
                onClick={() => void create()}
                disabled={!email || loading}
              >
                {loading ? "创建中…" : "创建邀请"}
              </Button>
            </div>
            {error && (
              <p
                role="alert"
                className="mt-3 text-sm text-[var(--neumo-danger)]"
              >
                {error}
              </p>
            )}
          </div>
        </section>

        {created && (
          <section className="neumo-raised rounded-3xl p-5 sm:p-6">
            <div className="flex items-center gap-2 text-sm font-semibold text-[var(--neumo-success)]">
              <Link2Icon className="size-4" />
              邀请已创建
            </div>
            <p className="mt-1 text-xs text-[var(--neumo-text-muted)]">
              链接只在这里完整显示，请立即复制并发送给用户。
            </p>
            <div className="mt-4 flex flex-col gap-2 sm:flex-row">
              <Input
                className="h-10 font-mono text-xs"
                readOnly
                value={created}
                onFocus={(e) => e.currentTarget.select()}
                aria-label="邀请链接"
              />
              <Button variant="outline" onClick={() => void copyLink()}>
                {copied ? <CheckIcon /> : <ClipboardIcon />}
                {copied ? "已复制" : "复制链接"}
              </Button>
            </div>
          </section>
        )}

        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold">邀请记录</h2>
            <span className="text-xs text-[var(--neumo-text-subtle)]">
              {items.length} 条
            </span>
          </div>
          <div className="neumo-raised overflow-hidden rounded-3xl p-2">
            {items.length === 0 ? (
              <div className="neumo-inset-sm flex flex-col items-center justify-center rounded-2xl px-6 py-12 text-center">
                <Link2Icon className="mb-3 size-7 text-[var(--neumo-text-subtle)]" />
                <p className="text-sm text-[var(--neumo-text-muted)]">
                  还没有邀请记录
                </p>
                <p className="mt-1 text-xs text-[var(--neumo-text-subtle)]">
                  创建第一条邀请，开始添加团队成员。
                </p>
              </div>
            ) : (
              <div className="divide-y divide-[var(--neumo-shadow)]/20">
                {items.map((item) => {
                  const status = statusLabel(item.status);
                  return (
                    <div
                      className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:justify-between"
                      key={item.id}
                    >
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">
                          {item.email}
                        </p>
                        <p className="mt-1 text-xs text-[var(--neumo-text-subtle)]">
                          创建于{" "}
                          {new Date(
                            item.created_at ?? item.expires_at,
                          ).toLocaleString()}{" "}
                          · 过期于 {new Date(item.expires_at).toLocaleString()}
                        </p>
                      </div>
                      <span
                        className={`neumo-inset-sm inline-flex w-fit items-center rounded-full px-3 py-1 text-xs font-medium ${status.color}`}
                      >
                        {status.label}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
