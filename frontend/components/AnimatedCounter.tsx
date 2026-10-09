"use client";

// Counts up to its target value on mount/update - GSAP tweening a plain
// object's number and writing it into the DOM directly (onUpdate), not
// React state per frame, so a fast count doesn't trigger dozens of re-renders.
import { useEffect, useRef } from "react";
import gsap from "gsap";

export default function AnimatedCounter({
  value, className,
}: { value: number; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const counter = { val: 0 };
    const tween = gsap.to(counter, {
      val: value,
      duration: 1.1,
      ease: "power2.out",
      onUpdate: () => {
        if (el) el.textContent = Math.round(counter.val).toString();
      },
    });
    return () => {
      tween.kill();
    };
  }, [value]);

  return <span ref={ref} className={className}>0</span>;
}
