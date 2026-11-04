"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";

export function ThemeProvider({
  children,
  ...props
}: React.ComponentProps<typeof NextThemesProvider>) {
  return (
    <NextThemesProvider
      {...props}
      // System theme is honored everywhere; users can still pick light,
      // dark, or follow-system from the appearance settings or the
      // header theme toggle.
    >
      {children}
    </NextThemesProvider>
  );
}
