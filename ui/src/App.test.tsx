import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { META, fakeApi, renderApp } from "./test/helpers";

describe("App shell", () => {
  it("names the project and navigates between views", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [], "/api/clusters": [] });
    renderApp("/");

    expect(await screen.findByText("acme-maintenance")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: "Clusters" }));

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Failure clusters");
    expect(screen.getByRole("link", { name: "Clusters" })).toHaveAttribute("aria-current", "page");
  });

  it("switches dark mode and remembers it", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [] });
    document.documentElement.classList.remove("dark");
    renderApp("/");

    await userEvent.click(screen.getByRole("button", { name: "Switch to dark mode" }));

    expect(document.documentElement).toHaveClass("dark");
    expect(localStorage.getItem("theme")).toBe("dark");
    await userEvent.click(screen.getByRole("button", { name: "Switch to light mode" }));
    expect(document.documentElement).not.toHaveClass("dark");
  });

  it("uses the server's user name until the reader types theirs", async () => {
    fakeApi({ "/api/meta": META, "/api/runs": [] });
    renderApp("/");
    const name = screen.getByLabelText("Your name for labels and reviews");
    await screen.findByDisplayValue("ada");

    await userEvent.clear(name);
    await userEvent.type(name, "grace");

    expect(localStorage.getItem("hecklr.user")).toBe("grace");
  });

  it("has a not-found page", async () => {
    fakeApi({ "/api/meta": META });
    renderApp("/nope");

    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });
});
