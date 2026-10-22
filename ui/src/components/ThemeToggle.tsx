import { useState } from "react";

type Theme = "light" | "dark";

function currentTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

/** Switches light and dark mode and remembers the choice (index.html applies it on load). */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="rounded-md border border-zinc-300 px-2 py-1 text-sm dark:border-zinc-700"
      aria-label={`Switch to ${next} mode`}
      onClick={() => {
        document.documentElement.classList.toggle("dark", next === "dark");
        try {
          localStorage.setItem("theme", next);
        } catch {
          // Blocked storage: the choice holds until the page is reloaded.
        }
        setTheme(next);
      }}
    >
      {theme === "dark" ? "Light" : "Dark"}
    </button>
  );
}
