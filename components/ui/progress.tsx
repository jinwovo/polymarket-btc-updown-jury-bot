import { cn } from "@/lib/utils";

interface ProgressProps {
  value: number;
  className?: string;
}

export function Progress({ value, className }: ProgressProps) {
  const width = Math.min(100, Math.max(0, value));
  return (
    <div className={cn("h-2.5 w-full overflow-hidden rounded-full bg-secondary/80", className)}>
      <div
        className="h-full rounded-full bg-gradient-to-r from-cyan-400 via-teal-400 to-orange-400 transition-all duration-500"
        style={{ width: `${width}%` }}
      />
    </div>
  );
}
