import "@/styles/globals.css";

import { type Metadata } from "next";

import { ScrollRevealInit } from "@/components/landing/scroll-reveal-init";
import { ThemeProvider } from "@/components/theme-provider";
import { DEFAULT_LOCALE } from "@/core/i18n/locale";

export const metadata: Metadata = {
  title: "OPIE OS",
  description:
    "OPIE OS is an AI agent platform self-developed by the OPIE team.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang={DEFAULT_LOCALE}
      suppressContentEditableWarning
      suppressHydrationWarning
    >
      <body>
        <ThemeProvider attribute="class" enableSystem disableTransitionOnChange>
          {children}
          <ScrollRevealInit />
        </ThemeProvider>
      </body>
    </html>
  );
}
