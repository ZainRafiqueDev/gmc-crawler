"use client";

// Cursor-following highlight on a glass-card (reactbits.dev-style flourish)
// - CSS does the actual rendering (.spotlight-card in app/globals.css), this
// component only tracks the pointer and writes two custom properties, so
// there's no per-frame React re-render on mousemove.
import { useRef } from "react";

export default function SpotlightCard({
  children, className = "",
}: { children: React.ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  function handleMouseMove(e: React.MouseEvent<HTMLDivElement>) {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    el.style.setProperty("--spot-x", `${e.clientX - rect.left}px`);
    el.style.setProperty("--spot-y", `${e.clientY - rect.top}px`);
  }

  return (
    <div ref={ref} onMouseMove={handleMouseMove} className={`spotlight-card glass-card rounded-2xl ${className}`}>
      {children}
    </div>
  );
}
