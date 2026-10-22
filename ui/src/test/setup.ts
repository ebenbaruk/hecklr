import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom does no layout, so it has no scrolling to do.
Element.prototype.scrollIntoView = vi.fn();

// Vitest runs without globals, so Testing Library cannot register its own cleanup.
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});
