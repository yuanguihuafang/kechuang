"use client";

import { useEffect } from "react";

/**
 * 全局滚动入屏渐入 —— 监听所有 `data-reveal` 元素。
 * 进入视口时加 `is-revealed` class，CSS 负责渐入动画。
 * 不可用 SSR 期间直接渲染（不影响 LCP）。
 */
export function ScrollRevealInit() {
  useEffect(() => {
    if (typeof window === "undefined") return;

    // 用户偏好减少动画 → 直接全部显示
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.querySelectorAll<HTMLElement>("[data-reveal]").forEach((el) => {
        el.classList.add("is-revealed");
      });
      return;
    }

    const targets = document.querySelectorAll<HTMLElement>("[data-reveal]");

    if (!("IntersectionObserver" in window)) {
      targets.forEach((el) => el.classList.add("is-revealed"));
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-revealed");
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.12, rootMargin: "0px 0px -8% 0px" },
    );

    targets.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, []);

  return null;
}
