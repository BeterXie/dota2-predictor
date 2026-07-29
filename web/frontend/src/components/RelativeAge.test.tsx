import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RelativeAge } from "./RelativeAge";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-07-30T12:00:05+00:00"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("RelativeAge", () => {
  it("updates only the subscribed age leaf", () => {
    let parentRenders = 0;
    function Parent() {
      parentRenders += 1;
      return (
        <div>
          <span>稳定内容</span>
          <RelativeAge
            observedAt="2026-07-30T12:00:00+00:00"
            prefix="赔率 "
          />
        </div>
      );
    }

    render(<Parent />);
    expect(screen.getByText("赔率 5 秒前")).toBeInTheDocument();
    expect(parentRenders).toBe(1);

    act(() => vi.advanceTimersByTime(2_000));

    expect(screen.getByText("赔率 7 秒前")).toBeInTheDocument();
    expect(parentRenders).toBe(1);
  });

  it("supports a fixed clock for deterministic consumers", () => {
    render(
      <RelativeAge
        className="age"
        now={Date.parse("2026-07-30T12:01:01+00:00")}
        observedAt="2026-07-30T12:00:00+00:00"
        staleAfterSeconds={60}
      />,
    );

    expect(screen.getByText("1 分钟前")).toHaveClass("age", "stale");
  });
});
