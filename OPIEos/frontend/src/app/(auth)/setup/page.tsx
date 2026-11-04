"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { RememberSessionOption } from "@/components/auth/remember-session-option";
import { OpieLogo } from "@/components/brand/opie-logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getCsrfHeaders } from "@/core/api/fetcher";
import { useAuth } from "@/core/auth/AuthProvider";
import { loadRememberLoginPreference } from "@/core/auth/remember-login";
import {
  fetchSetupStatus,
  isSystemAlreadyInitializedError,
} from "@/core/auth/setup";
import { parseAuthError } from "@/core/auth/types";
import { useI18n } from "@/core/i18n/hooks";

type SetupMode = "loading" | "init_admin" | "change_password" | "unavailable";

export default function SetupPage() {
  const router = useRouter();
  const { user, isAuthenticated } = useAuth();
  const { t } = useI18n();
  const [mode, setMode] = useState<SetupMode>("loading");
  const [setupStatusAttempt, setSetupStatusAttempt] = useState(0);

  // --- Shared state ---
  const [email, setEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [rememberMe, setRememberMe] = useState(
    () => loadRememberLoginPreference().rememberMe,
  );

  // --- Change-password mode only ---
  const [currentPassword, setCurrentPassword] = useState("");

  useEffect(() => {
    let cancelled = false;

    if (isAuthenticated && user?.needs_setup) {
      setMode("change_password");
    } else if (!isAuthenticated) {
      // Check if the system has no users yet. A slow Gateway must not leave the
      // setup page in an infinite loading state or silently redirect away.
      setMode("loading");
      void fetchSetupStatus()
        .then((data: { needs_setup?: boolean }) => {
          if (cancelled) return;
          if (data.needs_setup) {
            setMode("init_admin");
          } else {
            // System already set up and user is not logged in — go to login
            router.replace("/login");
          }
        })
        .catch(() => {
          if (!cancelled) setMode("unavailable");
        });
    } else {
      // Authenticated but needs_setup is false — already set up
      router.replace("/workspace");
    }

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, user, router, setupStatusAttempt]);

  // ── Init-admin handler ─────────────────────────────────────────────
  const handleInitAdmin = async (e: React.SubmitEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError("两次输入的密码不一致");
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/v1/auth/initialize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          email,
          password: newPassword,
          remember_me: rememberMe,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        if (isSystemAlreadyInitializedError(data)) {
          router.replace("/login");
          return;
        }
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      router.push("/workspace");
    } catch {
      setError("网络连接异常，请稍后重试。");
    } finally {
      setLoading(false);
    }
  };

  // ── Change-password handler ────────────────────────────────────────
  const handleChangePassword = async (e: React.SubmitEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError("两次输入的密码不一致");
      return;
    }
    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/v1/auth/change-password", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getCsrfHeaders(),
        },
        credentials: "include",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
          new_email: email || undefined,
          remember_me: rememberMe,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      router.push("/workspace");
    } catch {
      setError("网络连接异常，请稍后重试。");
    } finally {
      setLoading(false);
    }
  };

  if (mode === "loading") {
    return (
      <div className="neumo-surface flex min-h-screen items-center justify-center px-4">
        <div className="neumo-raised-sm rounded-2xl px-6 py-3 text-sm text-[color:var(--neumo-text-muted)]">
          正在加载…
        </div>
      </div>
    );
  }

  if (mode === "unavailable") {
    return (
      <div className="neumo-surface flex min-h-screen items-center justify-center px-4 py-12">
        <div className="neumo-raised w-full max-w-md space-y-6 rounded-3xl p-8 text-center">
          <div>
            <h1 className="text-xl font-semibold text-[color:var(--neumo-text)]">
              {t.login.serviceUnavailableTitle}
            </h1>
            <p className="mt-2 text-sm text-[color:var(--neumo-text-muted)]">
              {t.login.serviceUnavailableDescription}
            </p>
          </div>
          <div className="flex justify-center gap-3">
            <Button
              type="button"
              variant="ghost"
              className="neumo-btn-accent h-11 rounded-2xl px-5 text-sm font-medium"
              onClick={() => {
                setMode("loading");
                setSetupStatusAttempt((attempt) => attempt + 1);
              }}
            >
              {t.login.retry}
            </Button>
            <Button
              type="button"
              variant="ghost"
              className="neumo-btn h-11 rounded-2xl px-5 text-sm font-medium"
              onClick={() => router.replace("/login")}
            >
              {t.login.signIn}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  // ── Admin initialization form ──────────────────────────────────────
  if (mode === "init_admin") {
    return (
      <div className="neumo-surface flex min-h-screen items-center justify-center px-4 py-12">
        <div className="neumo-raised w-full max-w-md space-y-6 rounded-3xl p-8">
          <div className="text-center">
            <Link
              href="/"
              aria-label="返回首页"
              className="neumo-raised-sm mx-auto inline-flex items-center gap-2 rounded-2xl px-5 py-3 text-2xl font-bold text-[color:var(--neumo-text)]"
            >
              <OpieLogo className="size-7" />
              OPIE OS
            </Link>
            <p className="mt-4 text-sm text-[color:var(--neumo-text-muted)]">
              创建管理员账户
            </p>
            <p className="mt-1 text-xs text-[color:var(--neumo-text-subtle)]">
              设置管理员账户后即可开始使用。
            </p>
          </div>
          <form onSubmit={handleInitAdmin} className="space-y-3">
            <div className="flex flex-col space-y-1.5">
              <label
                htmlFor="email"
                className="text-sm font-medium text-[color:var(--neumo-text)]"
              >
                邮箱
              </label>
              <Input
                id="email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                className="neumo-input rounded-2xl px-4 py-3 text-sm"
              />
            </div>
            <div className="flex flex-col space-y-1.5">
              <label
                htmlFor="password"
                className="text-sm font-medium text-[color:var(--neumo-text)]"
              >
                密码
              </label>
              <Input
                id="password"
                type="password"
                placeholder="请输入密码，至少 8 个字符"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                minLength={8}
                className="neumo-input rounded-2xl px-4 py-3 text-sm"
              />
            </div>
            <div className="flex flex-col space-y-1.5">
              <label
                htmlFor="confirmPassword"
                className="text-sm font-medium text-[color:var(--neumo-text)]"
              >
                确认密码
              </label>
              <Input
                id="confirmPassword"
                type="password"
                placeholder="请再次输入密码"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                required
                minLength={8}
                className="neumo-input rounded-2xl px-4 py-3 text-sm"
              />
            </div>
            <RememberSessionOption
              checked={rememberMe}
              onCheckedChange={setRememberMe}
            />
            {error && (
              <div className="neumo-inset-sm rounded-xl px-4 py-2.5 text-sm text-[color:var(--neumo-danger)]">
                {error}
              </div>
            )}
            <Button
              type="submit"
              disabled={loading}
              className="neumo-btn-accent mt-2 h-12 w-full rounded-2xl text-sm font-medium"
            >
              {loading ? "正在创建账户…" : "创建管理员账户"}
            </Button>
          </form>
        </div>
      </div>
    );
  }

  // ── Change-password form (needs_setup after login) ─────────────────
  return (
    <div className="neumo-surface flex min-h-screen items-center justify-center px-4 py-12">
      <div className="neumo-raised w-full max-w-md space-y-6 rounded-3xl p-8">
        <div className="text-center">
          <Link
            href="/"
            aria-label="返回首页"
            className="neumo-raised-sm mx-auto inline-flex items-center gap-2 rounded-2xl px-5 py-3 text-2xl font-bold text-[color:var(--neumo-text)]"
          >
            <OpieLogo className="size-7" />
            OPIE OS
          </Link>
          <p className="mt-4 text-sm text-[color:var(--neumo-text-muted)]">
            完成管理员账户设置
          </p>
          <p className="mt-1 text-xs text-[color:var(--neumo-text-subtle)]">
            请设置常用邮箱和新密码。
          </p>
        </div>
        <form onSubmit={handleChangePassword} className="space-y-3">
          <Input
            type="email"
            placeholder="请输入邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="neumo-input rounded-2xl px-4 py-3 text-sm"
          />
          <Input
            type="password"
            placeholder="请输入当前密码"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
            className="neumo-input rounded-2xl px-4 py-3 text-sm"
          />
          <Input
            type="password"
            placeholder="请输入新密码"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            minLength={8}
            className="neumo-input rounded-2xl px-4 py-3 text-sm"
          />
          <Input
            type="password"
            placeholder="请再次输入新密码"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            minLength={8}
            className="neumo-input rounded-2xl px-4 py-3 text-sm"
          />
          <RememberSessionOption
            checked={rememberMe}
            onCheckedChange={setRememberMe}
          />
          {error && (
            <div className="neumo-inset-sm rounded-xl px-4 py-2.5 text-sm text-[color:var(--neumo-danger)]">
              {error}
            </div>
          )}
          <Button
            type="submit"
            disabled={loading}
            className="neumo-btn-accent mt-2 h-12 w-full rounded-2xl text-sm font-medium"
          >
            {loading ? "正在完成设置…" : "完成设置"}
          </Button>
        </form>
      </div>
    </div>
  );
}
