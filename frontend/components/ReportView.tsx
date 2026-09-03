"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { motion, AnimatePresence } from "framer-motion";

// A report with many hundreds of findings (e.g. a large, messy real site)
// produces enough Markdown that rendering it all as DOM nodes at once can
// visibly stall the tab. Cap what's rendered inline and point at the
// full-content download buttons instead - the download always has everything.
const INLINE_RENDER_LIMIT = 150_000;

// The report (app/report.py) always emits "## Other Findings (Lower
// Priority)" as one self-contained section ending at the next "## " heading
// - split it out and render it collapsed by default, so the suspension-risk
// section above it reads as the primary view and lower-priority findings
// are a deliberate, visible-on-demand secondary view rather than an
// undifferentiated flat list.
const SECONDARY_HEADING = "## Other Findings (Lower Priority)";

function splitOutSecondarySection(markdown: string): { primary: string; secondary: string | null; rest: string } {
  const start = markdown.indexOf(SECONDARY_HEADING);
  if (start === -1) return { primary: markdown, secondary: null, rest: "" };

  const afterHeading = markdown.slice(start + SECONDARY_HEADING.length);
  const nextHeadingMatch = afterHeading.match(/\n## /);
  const sectionBody = nextHeadingMatch ? afterHeading.slice(0, nextHeadingMatch.index) : afterHeading;
  const rest = nextHeadingMatch ? afterHeading.slice(nextHeadingMatch.index! + 1) : "";

  return {
    primary: markdown.slice(0, start),
    secondary: sectionBody.trim(),
    rest,
  };
}

const SEVERITY_CLASS: Record<string, string> = {
  critical: "bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300",
  medium: "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  low: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
};

// Colors the leading "[CRITICAL]"/"[HIGH]"/... tag emitted by
// _format_finding_compact (app/report.py) as a pill instead of plain text,
// so the page-by-page section is scannable at a glance.
function colorizeSeverityTag(text: string): React.ReactNode {
  const match = /^\[(CRITICAL|HIGH|MEDIUM|LOW)\]\s*/.exec(text);
  if (!match) return text;
  const level = match[1].toLowerCase();
  const rest = text.slice(match[0].length);
  return (
    <>
      <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-semibold mr-1.5 align-middle ${SEVERITY_CLASS[level]}`}>
        {match[1]}
      </span>
      {rest}
    </>
  );
}

const components: Components = {
  li({ children, ...props }) {
    const first = Array.isArray(children) ? children[0] : children;
    if (typeof first === "string" && /^\[(CRITICAL|HIGH|MEDIUM|LOW)\]/.test(first)) {
      const rest = Array.isArray(children) ? children.slice(1) : [];
      return (
        <li {...props}>
          {colorizeSeverityTag(first)}
          {rest}
        </li>
      );
    }
    return <li {...props}>{children}</li>;
  },
};

function Markdown({ text }: { text: string }) {
  return (
    <div className="prose prose-slate dark:prose-invert max-w-none prose-headings:font-semibold prose-h1:text-2xl prose-h2:text-xl prose-h2:border-b prose-h2:border-surface-border prose-h2:pb-2 prose-h3:text-lg prose-strong:font-semibold">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export default function ReportView({ markdown }: { markdown: string }) {
  const [expanded, setExpanded] = useState(false);
  const truncated = markdown.length > INLINE_RENDER_LIMIT;
  const shown = truncated ? markdown.slice(0, INLINE_RENDER_LIMIT) : markdown;
  const { primary, secondary, rest } = splitOutSecondarySection(shown);

  return (
    <div>
      {truncated && (
        <div className="bg-amber-50 border border-amber-200 rounded-md px-3 py-2 text-sm text-amber-800 mb-4">
          This report is large ({markdown.length.toLocaleString()} characters) - showing the first part
          here. Use the download buttons above for the complete report.
        </div>
      )}
      <Markdown text={primary} />
      {secondary !== null && (
        <div className="mt-4 border border-surface-border rounded-lg overflow-hidden">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="w-full flex items-center justify-between px-4 py-2.5 text-sm font-medium bg-brand-1-soft/60 hover:bg-brand-1-soft transition-colors"
          >
            <span>Other Findings (Lower Priority) - click to {expanded ? "collapse" : "expand"}</span>
            <motion.span animate={{ rotate: expanded ? 180 : 0 }} transition={{ duration: 0.2 }}>
              ▾
            </motion.span>
          </button>
          <AnimatePresence initial={false}>
            {expanded && (
              <motion.div
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
                className="overflow-hidden"
              >
                <div className="px-4 pt-3">
                  <Markdown text={secondary} />
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}
      {rest && (
        <div className="mt-4">
          <Markdown text={rest} />
        </div>
      )}
    </div>
  );
}
