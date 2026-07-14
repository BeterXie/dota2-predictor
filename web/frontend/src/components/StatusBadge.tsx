import { Badge } from "@fluentui/react-components";
import type { BadgeProps } from "@fluentui/react-components";

import { lifecycleLabel, readinessLabel } from "../format";
import type { Lifecycle, ReadinessStatus } from "../types";

const lifecycleColor: Record<Lifecycle, BadgeProps["color"]> = {
  live: "success",
  degraded: "warning",
  upcoming: "informative",
  ended: "subtle",
};

const readinessColor: Record<ReadinessStatus, BadgeProps["color"]> = {
  ready: "success",
  delayed: "warning",
  stale: "danger",
  missing: "subtle",
  invalid: "danger",
  unconfirmed: "warning",
  degraded: "warning",
  unhealthy: "danger",
  stopped: "subtle",
};

export function LifecycleBadge({ lifecycle }: { lifecycle: Lifecycle }) {
  return (
    <Badge appearance="tint" color={lifecycleColor[lifecycle]} size="small">
      {lifecycleLabel[lifecycle]}
    </Badge>
  );
}

export function ReadinessBadge({ status }: { status: ReadinessStatus }) {
  return (
    <Badge appearance="tint" color={readinessColor[status]} size="small">
      {readinessLabel[status]}
    </Badge>
  );
}
