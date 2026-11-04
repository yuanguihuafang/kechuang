"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { resolveAuthNextPath } from "@/core/auth/next-path";

export default function AuthCallbackPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<"loading" | "success" | "error">(
    "loading",
  );
  const calledRef = useRef(false);

  const doAuthCheck = useCallback(async () => {
    if (calledRef.current) return;
    calledRef.current = true;

    const next = resolveAuthNextPath(searchParams.get("next"));

    try {
      const res = await fetch("/api/v1/auth/me", { credentials: "include" });

      if (res.ok) {
        setStatus("success");
        // Small delay so the user sees the success message
        setTimeout(() => router.replace(next), 300);
      } else {
        setStatus("error");
        setTimeout(() => router.replace("/login?error=sso_failed"), 1500);
      }
    } catch {
      setStatus("error");
      setTimeout(() => router.replace("/login?error=sso_failed"), 1500);
    }
  }, [searchParams, router]);

  useEffect(() => {
    void doAuthCheck();
  }, [doAuthCheck]);

  return (
    <div className="bg-background relative flex min-h-screen items-center justify-center">
      <div className="text-center">
        {status === "loading" && (
          <>
            <div className="mx-auto mb-4 h-8 w-8 animate-spin rounded-full border-2 border-current border-t-transparent" />
            <p className="text-muted-foreground">正在登录…</p>
          </>
        )}
        {status === "success" && (
          <p className="text-muted-foreground">正在跳转…</p>
        )}
        {status === "error" && (
          <p className="text-muted-foreground">
            身份验证失败，正在跳转回登录页…
          </p>
        )}
      </div>
    </div>
  );
}
