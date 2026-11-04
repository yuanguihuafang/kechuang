"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { RememberSessionOption } from "@/components/auth/remember-session-option";
import { OpieLogo } from "@/components/brand/opie-logo";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useAuth } from "@/core/auth/AuthProvider";
import { resolveAuthNextPath } from "@/core/auth/next-path";
import {
  loadRememberLoginPreference,
  saveRememberLoginPreference,
} from "@/core/auth/remember-login";
import {
  canCreateRegularAccount,
  fetchSetupStatus,
  type SetupStatusResponse,
} from "@/core/auth/setup";
import { parseAuthError } from "@/core/auth/types";
import { useI18n } from "@/core/i18n/hooks";

export default function LoginPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { isAuthenticated } = useAuth();
  const { t } = useI18n();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(true);
  const inviteToken = searchParams.get("invite");
  const [isLogin, setIsLogin] = useState(() => !inviteToken);
  const [ssoProviders, setSsoProviders] = useState<
    { id: string; display_name: string; type: string }[]
  >([]);
  const [setupStatus, setSetupStatus] = useState<SetupStatusResponse | null>(
    null,
  );
  const [setupStatusPhase, setSetupStatusPhase] = useState<
    "checking" | "ready" | "unavailable"
  >("checking");
  const [setupStatusAttempt, setSetupStatusAttempt] = useState(0);

  // Extract error from query params (e.g., ?error=sso_failed)
  const errorParam = searchParams.get("error");
  const [error, setError] = useState(
    errorParam
      ? (t.login.errors[errorParam as keyof typeof t.login.errors] ??
          t.login.authFailed)
      : "",
  );
  // Soft hint shown after a failed login when SSO is configured: an SSO-only
  // account has no local password, so the backend returns a generic
  // "incorrect email or password" (deliberately, to avoid account enumeration).
  // Nudge the user toward the SSO buttons without confirming the account exists.
  const [showSsoHint, setShowSsoHint] = useState(false);
  const [loading, setLoading] = useState(false);

  // Get next parameter for validated redirect
  const nextParam = searchParams.get("next");
  const redirectPath = resolveAuthNextPath(nextParam);
  // An invitation is the explicit capability grant. The backend remains the
  // source of truth for token, expiry, email, and one-time-use validation.
  const regularSignupAllowed = Boolean(inviteToken) || canCreateRegularAccount({
    checked: setupStatusPhase === "ready",
    status: setupStatus,
  });
  const systemNeedsAdminSetup = setupStatus?.needs_setup === true;
  const showSetupStatusUnavailable =
    setupStatusPhase === "unavailable" ||
    (setupStatusAttempt > 0 && setupStatusPhase === "checking");

  // Redirect if already authenticated (client-side, post-login)
  useEffect(() => {
    if (isAuthenticated) {
      router.push(redirectPath);
    }
  }, [isAuthenticated, redirectPath, router]);

  useEffect(() => {
    const preference = loadRememberLoginPreference();
    setRememberMe(preference.rememberMe);
    if (preference.email) {
      setEmail(preference.email);
    }
  }, []);

  // Fetch setup state independently so retrying a slow Gateway does not also
  // refetch unrelated auth-provider configuration.
  useEffect(() => {
    let cancelled = false;
    setSetupStatusPhase("checking");

    void fetchSetupStatus()
      .then((data) => {
        if (cancelled) return;
        setSetupStatus(data);
        setSetupStatusPhase("ready");
        if (data.needs_setup && !inviteToken) {
          setIsLogin(true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSetupStatus(null);
          setSetupStatusPhase("unavailable");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [inviteToken, setupStatusAttempt]);

  // SSO providers are static for the page lifetime and should not be coupled to
  // setup-status retries.
  useEffect(() => {
    let cancelled = false;

    void fetch("/api/v1/auth/providers")
      .then((r) => r.json())
      .then(
        (data: {
          providers: { id: string; display_name: string; type: string }[];
        }) => {
          if (!cancelled) {
            setSsoProviders(data.providers ?? []);
          }
        },
      )
      .catch(() => {
        // Ignore errors; no SSO providers shown
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setShowSsoHint(false);
    setLoading(true);

    if (!isLogin && !regularSignupAllowed) {
      setError(t.login.adminSetupRequiredDescription);
      setLoading(false);
      return;
    }

    try {
      const endpoint = isLogin
        ? "/api/v1/auth/login/local"
        : `/api/v1/auth/register-by-invite?invite=${encodeURIComponent(inviteToken ?? "")}`;
      const body = isLogin
        ? new URLSearchParams({
            password,
            remember_me: String(rememberMe),
            username: email,
          })
        : JSON.stringify({ email, password, remember_me: rememberMe });

      const headers: HeadersInit = isLogin
        ? { "Content-Type": "application/x-www-form-urlencoded" }
        : { "Content-Type": "application/json" };

      const res = await fetch(endpoint, {
        method: "POST",
        headers,
        body,
        credentials: "include", // Important: include HttpOnly cookie
      });

      if (!res.ok) {
        const data = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        // On a failed login with SSO configured, surface a hint pointing at the
        // SSO buttons — the "wrong password" may really mean "this is an SSO account".
        if (isLogin && ssoProviders.length > 0) {
          setShowSsoHint(true);
        }
        return;
      }

      saveRememberLoginPreference({ email, rememberMe });

      // Both login and register set a cookie — redirect to workspace
      router.push(redirectPath);
    } catch {
      setError(t.login.networkError);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="neumo-surface relative flex min-h-screen items-center justify-center overflow-x-hidden overflow-y-auto px-4 py-12">
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
            {isLogin ? t.login.signInTitle : t.login.createAccountTitle}
          </p>
        </div>

        {showSetupStatusUnavailable && (
          <div
            role="status"
            aria-live="polite"
            className="neumo-inset-sm space-y-2 rounded-2xl p-4 text-sm text-[color:var(--neumo-text)]"
          >
            <p className="font-medium">{t.login.serviceUnavailableTitle}</p>
            <p className="text-[color:var(--neumo-text-muted)]">
              {t.login.serviceUnavailableDescription}
            </p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="neumo-btn mt-2 rounded-xl px-3 py-1.5 text-xs"
              disabled={setupStatusPhase === "checking"}
              onClick={() => {
                setSetupStatusPhase("checking");
                setSetupStatusAttempt((attempt) => attempt + 1);
              }}
            >
              {setupStatusPhase === "checking"
                ? t.login.pleaseWait
                : t.login.retry}
            </Button>
          </div>
        )}

        {systemNeedsAdminSetup && (
          <div className="neumo-inset-sm rounded-2xl p-4 text-sm text-[color:var(--neumo-text)]">
            <p className="font-medium">{t.login.adminSetupRequiredTitle}</p>
            <p className="mt-1 text-[color:var(--neumo-text-muted)]">
              {t.login.adminSetupRequiredDescription}
            </p>
            <Link
              href="/setup"
              className="mt-2 inline-block font-medium text-[color:var(--neumo-accent-text)]"
            >
              {t.login.createAdminAccount}
            </Link>
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-2">
          <div className="flex flex-col space-y-1">
            <label htmlFor="email" className="text-sm font-medium">
              {t.login.email}
            </label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder={t.login.emailPlaceholder}
              required
              className="neumo-input rounded-2xl px-4 py-3 text-sm"
            />
          </div>
          <div className="flex flex-col space-y-1">
            <label htmlFor="password" className="text-sm font-medium">
              {t.login.password}
            </label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t.login.passwordPlaceholder}
              required
              minLength={isLogin ? 6 : 8}
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
            {loading
              ? t.login.pleaseWait
              : isLogin
                ? t.login.signIn
                : t.login.createAccount}
          </Button>
        </form>

        {ssoProviders.length > 0 && (
          <div className="space-y-2">
            {isLogin && (
              <div className="relative my-4 flex items-center gap-3">
                <div className="neumo-inset-sm h-px flex-1 rounded-full" />
                <span className="text-xs tracking-widest text-[color:var(--neumo-text-subtle)] uppercase">
                  {t.login.orContinueWith}
                </span>
                <div className="neumo-inset-sm h-px flex-1 rounded-full" />
              </div>
            )}
            {showSsoHint && (
              <p className="text-center text-sm text-[color:var(--neumo-text-muted)]">
                {t.login.ssoHint}
              </p>
            )}
            {ssoProviders.map((provider) => (
              <Button
                key={provider.id}
                type="button"
                variant="ghost"
                disabled={loading}
                className="neumo-btn h-12 w-full rounded-2xl text-sm font-medium"
                onClick={() => {
                  window.location.href = `/api/v1/auth/oauth/${provider.id}?next=${encodeURIComponent(redirectPath)}&remember_me=${String(rememberMe)}`;
                }}
              >
                {t.login.continueWith(provider.display_name)}
              </Button>
            ))}
          </div>
        )}

        {regularSignupAllowed && (
          <div className="text-center text-sm">
            <button
              type="button"
              onClick={() => {
                setIsLogin(!isLogin);
                setError("");
                setShowSsoHint(false);
              }}
              className="font-medium text-[color:var(--neumo-accent-text)] hover:underline"
            >
              {isLogin ? t.login.noAccountSignUp : t.login.haveAccountSignIn}
            </button>
          </div>
        )}

        <div className="text-center text-xs text-[color:var(--neumo-text-subtle)]">
          <Link
            href="/"
            className="hover:text-[color:var(--neumo-text)] hover:underline"
          >
            {t.login.backToHome}
          </Link>
        </div>
      </div>
    </div>
  );
}
