// Issue #110: every grey (zinc) text class in the UI meets WCAG 2.1 AA (4.5:1) in both themes, on
// the surfaces grey text sits on, or on the element's own zinc background when it sets one. The
// colours come from Tailwind's own theme file, so the check follows a Tailwind upgrade.
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const AA_TEXT = 4.5;
const SRC = join(__dirname);
const THEME = join(__dirname, "..", "node_modules", "tailwindcss", "theme.css");

/** Surfaces grey text is drawn on: page and card, plus the transcript bubbles and table stripes. */
const LIGHT_SURFACES = ["white", "50", "100"];
const DARK_SURFACES = ["950", "900", "800"];

function zincLuminances(): Map<string, number> {
  const theme = readFileSync(THEME, "utf-8");
  const shades = new Map<string, number>([["white", 1]]);
  for (const [, shade = "", l = "", c = "", h = ""] of theme.matchAll(
    /--color-zinc-(\d+): oklch\(([\d.]+)% ([\d.]+) ([\d.]+|none)\)/g,
  )) {
    shades.set(shade, oklchLuminance(Number(l) / 100, Number(c), h === "none" ? 0 : Number(h)));
  }
  return shades;
}

/** Relative luminance (WCAG) of an OKLCH colour, through linear sRGB. */
function oklchLuminance(l: number, c: number, hueDegrees: number): number {
  const hue = (hueDegrees * Math.PI) / 180;
  const a = c * Math.cos(hue);
  const b = c * Math.sin(hue);
  const lms = [
    (l + 0.3963377774 * a + 0.2158037573 * b) ** 3,
    (l - 0.1055613458 * a - 0.0638541728 * b) ** 3,
    (l - 0.0894841775 * a - 1.291485548 * b) ** 3,
  ] as const;
  const clamp = (x: number) => Math.min(1, Math.max(0, x));
  const red = clamp(4.0767416621 * lms[0] - 3.3077115913 * lms[1] + 0.2309699292 * lms[2]);
  const green = clamp(-1.2684380046 * lms[0] + 2.6097574011 * lms[1] - 0.3413193965 * lms[2]);
  const blue = clamp(-0.0041960863 * lms[0] - 0.7034186147 * lms[1] + 1.707614701 * lms[2]);
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function contrast(first: number, second: number): number {
  const [high, low] = first > second ? [first, second] : [second, first];
  return (high + 0.05) / (low + 0.05);
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "test" ? [] : sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

/** Every string or template literal holding a zinc text class, with where it is. */
function classLists(): { where: string; classes: string[] }[] {
  return sourceFiles(SRC).flatMap((file) =>
    [...readFileSync(file, "utf-8").matchAll(/"([^"\n]*)"|`([^`]*)`/g)]
      .map((m) => ({ where: `${file.slice(SRC.length + 1)}: ${m[0]}`, classes: (m[1] ?? m[2] ?? "").split(/\s+/) }))
      .filter(({ classes }) => classes.some((c) => /^(dark:)?text-zinc-\d+$/.test(c))),
  );
}

function shade(classes: string[], prefix: string): string | undefined {
  for (const c of classes) {
    const match = new RegExp(`^${prefix}-zinc-(\\d+)$`).exec(c);
    if (match) return match[1];
  }
  return undefined;
}

describe("colour contrast", () => {
  const zinc = zincLuminances();
  const luminance = (shadeName: string): number => {
    const value = zinc.get(shadeName);
    if (value === undefined) throw new Error(`zinc-${shadeName} is not in Tailwind's theme`);
    return value;
  };

  it("matches the WCAG reference values", () => {
    expect(contrast(0, 1)).toBeCloseTo(21);
    expect(contrast(oklchLuminance(1, 0, 0), 1)).toBeCloseTo(1);
    expect(zinc.get("500")).toBeDefined();
  });

  it("issue110: grey text meets WCAG AA in light and dark mode", () => {
    const failures: string[] = [];
    const lists = classLists();
    for (const { where, classes } of lists) {
      const light = shade(classes, "text");
      const dark = shade(classes, "dark:text") ?? light;
      const lightSurfaces = [shade(classes, "bg") ?? LIGHT_SURFACES].flat();
      const darkSurfaces = [shade(classes, "dark:bg") ?? DARK_SURFACES].flat();
      const pairs: [string, string | undefined, string[]][] = [
        ["light", light, lightSurfaces],
        ["dark", dark, darkSurfaces],
      ];
      for (const [theme, text, surfaces] of pairs) {
        if (text === undefined) continue;
        for (const surface of surfaces) {
          const ratio = contrast(luminance(text), luminance(surface));
          if (ratio < AA_TEXT) failures.push(`${theme} zinc-${text} on ${surface} ${ratio.toFixed(2)}:1 in ${where}`);
        }
      }
    }
    expect(lists.length).toBeGreaterThan(40);
    expect(failures).toEqual([]);
  });
});
