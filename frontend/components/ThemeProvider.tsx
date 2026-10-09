"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";

// attribute="class" + the two theme names below map directly to the
// `.dark`/`.light` selectors in app/globals.css (and to Tailwind's
// `@custom-variant dark (&:where(.dark, .dark *))`, also defined there).
// enableSystem is off deliberately: the user picked a single hard default
// (dark) with a manual toggle, not "follow the visitor's OS setting".
export default function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider attribute="class" defaultTheme="dark" enableSystem={false} themes={["dark", "light"]}>
      {children}
    </NextThemesProvider>
  );
}
