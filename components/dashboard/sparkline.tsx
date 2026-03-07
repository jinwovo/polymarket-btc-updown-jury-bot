"use client";

import { cn } from "@/lib/utils";

interface SparklineProps {
  values: number[];
  stroke: string;
  className?: string;
  fillGradient?: string;
}

export function Sparkline({ values, stroke, className, fillGradient }: SparklineProps) {
  const width = 960;
  const height = 180;
  const padding = 10;

  if (!values || values.length < 2) {
    return (
      <div
        className={cn(
          "flex h-[180px] items-center justify-center rounded-xl border border-border/70 bg-background/30 text-sm text-muted-foreground",
          className,
        )}
      >
        No data yet
      </div>
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const plotW = width - padding * 2;
  const plotH = height - padding * 2;

  const points = values.map((v, idx) => {
    const x = padding + (idx / Math.max(values.length - 1, 1)) * plotW;
    const y = padding + (1 - (v - min) / span) * plotH;
    return `${x},${y}`;
  });

  const d = points.reduce((acc, p, idx) => `${acc}${idx === 0 ? "M" : " L"}${p}`, "");
  const last = points[points.length - 1].split(",").map(Number);
  const area = `${d} L ${width - padding},${height - padding} L ${padding},${height - padding} Z`;

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className={cn("h-[180px] w-full rounded-xl border border-border/70 bg-background/30", className)}
      preserveAspectRatio="none"
    >
      <defs>
        <linearGradient id={`grad-${stroke.replace(/[^a-z0-9]/gi, "")}`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={fillGradient ?? stroke} stopOpacity={0.32} />
          <stop offset="100%" stopColor={fillGradient ?? stroke} stopOpacity={0} />
        </linearGradient>
      </defs>
      {[0.2, 0.4, 0.6, 0.8].map((n) => (
        <line
          key={n}
          x1={padding}
          x2={width - padding}
          y1={padding + n * plotH}
          y2={padding + n * plotH}
          stroke="rgba(148,163,184,.22)"
          strokeWidth={1}
        />
      ))}
      <path d={area} fill={`url(#grad-${stroke.replace(/[^a-z0-9]/gi, "")})`} />
      <path d={d} fill="none" stroke={stroke} strokeWidth={3} strokeLinecap="round" />
      <circle cx={last[0]} cy={last[1]} r={4} fill={stroke} />
    </svg>
  );
}
