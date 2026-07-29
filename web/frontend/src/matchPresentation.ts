import type { MonitorMatch, VisionPoint } from "./types";

export function getTrustedVision(match: MonitorMatch): VisionPoint | null {
  const vision = match.latest_vision;
  const status = match.readiness.vision.status;
  return vision?.confirmed === 1 && (status === "ready" || status === "delayed")
    ? vision
    : null;
}
