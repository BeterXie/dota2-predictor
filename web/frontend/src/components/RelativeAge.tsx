import type { ReactNode } from "react";

import { ageSeconds, formatAge, parseTimestamp } from "../format";
import { useLiveClock } from "../liveClock";

interface RelativeAgeProps {
  className?: string;
  icon?: ReactNode;
  now?: number;
  observedAt?: string | null;
  prefix?: string;
  staleAfterSeconds?: number;
}

export function RelativeAge({
  className,
  icon,
  now: nowOverride,
  observedAt,
  prefix = "",
  staleAfterSeconds,
}: RelativeAgeProps) {
  const hasTimestamp = Boolean(observedAt && parseTimestamp(observedAt));
  const now = useLiveClock(hasTimestamp ? nowOverride : 0);
  const age = ageSeconds(observedAt, now);
  const stale = staleAfterSeconds != null && age != null && age > staleAfterSeconds;
  const classes = [className, stale ? "stale" : ""].filter(Boolean).join(" ");

  return (
    <span className={classes || undefined}>
      {icon}
      {prefix}{formatAge(age)}
    </span>
  );
}
