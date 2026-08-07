import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

globalThis.NodeFilter = window.NodeFilter;

afterEach(async () => {
  cleanup();
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
});
