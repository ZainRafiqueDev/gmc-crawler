"use client";

import { useSyncExternalStore } from "react";
import { useTheme } from "next-themes";
import { AnimatePresence, motion } from "framer-motion";
import { Moon, Sun } from "lucide-react";

const noopSubscribe = () => () => {};

// Theme is unknown during SSR/first paint (next-themes resolves it from
// localStorage client-side only) - rendering a fixed-size placeholder until
// mounted avoids a hydration mismatch and avoids the button flashing the
// wrong icon for a frame. useSyncExternalStore (not a did-mount effect +
// setState) is the idiomatic way to read "is this the client's first paint
// yet": the server snapshot is always false, the client snapshot is always
// true, so React reconciles the difference on hydration instead of a
// post-mount re-render.
export default function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const mounted = useSyncExternalStore(noopSubscribe, () => true, () => false);

  if (!mounted) {
    return <span className="inline-block h-9 w-9" aria-hidden="true" />;
  }

  const isDark = theme === "dark";

  return (
    <motion.button
      onClick={() => setTheme(isDark ? "light" : "dark")}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      whileHover={{ scale: 1.06 }}
      whileTap={{ scale: 0.92 }}
      className="relative h-9 w-9 flex items-center justify-center rounded-full border border-surface-border bg-surface/60 hover:bg-brand-1-soft overflow-hidden transition-colors"
    >
      <AnimatePresence mode="wait" initial={false}>
        {isDark ? (
          <motion.span
            key="sun"
            initial={{ rotate: -90, opacity: 0, scale: 0.4 }}
            animate={{ rotate: 0, opacity: 1, scale: 1 }}
            exit={{ rotate: 90, opacity: 0, scale: 0.4 }}
            transition={{ type: "spring", stiffness: 300, damping: 20 }}
            className="absolute inset-0 flex items-center justify-center text-amber-400"
          >
            <Sun size={18} strokeWidth={2} />
          </motion.span>
        ) : (
          <motion.span
            key="moon"
            initial={{ rotate: 90, opacity: 0, scale: 0.4 }}
            animate={{ rotate: 0, opacity: 1, scale: 1 }}
            exit={{ rotate: -90, opacity: 0, scale: 0.4 }}
            transition={{ type: "spring", stiffness: 300, damping: 20 }}
            className="absolute inset-0 flex items-center justify-center"
            style={{ color: "var(--brand-1)" }}
          >
            <Moon size={18} strokeWidth={2} />
          </motion.span>
        )}
      </AnimatePresence>
    </motion.button>
  );
}
