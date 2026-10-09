"use client";

// GSAP ScrollTrigger entrance - every section that uses this fades/slides in
// once as it enters the viewport, scroll-driven rather than time-driven
// (unlike the Framer Motion stagger variants used for in-view-on-load lists
// elsewhere in this app, e.g. lib/motion.ts). Used for page-section-level
// reveals on longer, "extensive website"-style pages; Framer Motion stays
// the tool for component-level micro-interactions (buttons, hover, stagger).
import { useLayoutEffect, useRef } from "react";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";

if (typeof window !== "undefined") {
  gsap.registerPlugin(ScrollTrigger);
}

export default function ScrollReveal({
  children, delay = 0, className, y = 32,
}: { children: React.ReactNode; delay?: number; className?: string; y?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ctx = gsap.context(() => {
      gsap.fromTo(
        el,
        { opacity: 0, y },
        {
          opacity: 1,
          y: 0,
          duration: 0.8,
          delay,
          ease: "power3.out",
          scrollTrigger: { trigger: el, start: "top 85%", once: true },
        },
      );
    }, ref);
    return () => ctx.revert();
  }, [delay, y]);

  return (
    <div ref={ref} className={className}>
      {children}
    </div>
  );
}
