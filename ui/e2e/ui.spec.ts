import { expect, type Page, test } from "@playwright/test";

// Drives a running `hecklr ui` in a real browser. The store must hold two runs of one suite
// (baseline older, candidate with failures), a run with judge grades, and miner output with
// at least two open proposals: what e2e/README.md's recipe produces. Each step saves a
// screenshot to HECKLR_E2E_SHOTS when set. Voice: set HECKLR_E2E_VOICE_TRIAL=<run>/<trial>.

const SHOTS = process.env.HECKLR_E2E_SHOTS;
const VOICE = process.env.HECKLR_E2E_VOICE_TRIAL;
const CI_RANGE = /\d+\.\d% \[\d+\.\d%, \d+\.\d%\]/;

async function shot(page: Page, name: string): Promise<void> {
  if (SHOTS) {
    await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
  }
}

test.describe.configure({ mode: "serial" });

test.describe("chat store", () => {
  test.skip(Boolean(VOICE), "voice mode runs only the waveform check");

  test("runs list shows every run with pass rate and cost intervals", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
    const rows = page.locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    await expect(rows.first().locator("td").nth(7)).toHaveText(CI_RANGE);
    await expect(rows.first().locator("td").nth(8)).toContainText("[$");
    await shot(page, "01-runs");
  });

  test("comparison shows verdict, delta with CI and flips grouped by cause", async ({ page }) => {
    await page.goto("/");
    const smoke = page.locator("tbody tr", { hasText: "smoke" });
    await smoke.nth(0).getByRole("checkbox").check();
    await smoke.nth(1).getByRole("checkbox").check();
    await page.getByRole("button", { name: /Compare 2\/2/ }).click();
    await expect(page).toHaveURL(/\/compare\?baseline=.+&candidate=.+/);
    const verdict = page.locator("section", { hasText: "Suite verdict" });
    await expect(verdict).toContainText("regression");
    await expect(verdict).toContainText(/Δ −\d+\.\d pts \[−\d+\.\d pts, −\d+\.\d pts\]/);
    const flips = page.locator("section", { hasText: "Flipped scenarios" });
    await expect(flips).toContainText("pass → fail");
    await shot(page, "02-compare");

    await flips.getByRole("link").first().click();
    await expect(page).toHaveURL(/\/trials\//);
    await expect(page.locator("section", { hasText: "Transcript" }).first()).toBeVisible();
    await expect(page.locator("section", { hasText: "Timeline" })).toContainText("call ");
    await expect(page.locator("section", { hasText: "World state" })).toBeVisible();
    await expect(page.locator("section", { hasText: "Grades" })).toContainText("fail");
    await shot(page, "03-trial");
  });

  test("labeling: blind verdict, label persists, agreement appears", async ({ page }) => {
    await page.goto("/labels");
    await expect(page.getByText(/hidden until you label/)).toBeVisible();
    await shot(page, "04-labels-before");
    await page.getByLabel("Note (optional)").fill("checked by the e2e test");
    await page.getByRole("button", { name: /^Fail/ }).click();
    await expect(page.getByText(/labeled 1/)).toBeVisible();
    const agreement = page.locator("section", { hasText: "Judge agreement" });
    await expect(agreement.locator("tbody tr").first()).toBeVisible();
    await expect(agreement.locator("tbody tr").first()).toContainText(/\d\.\d\d \[/);
    await page.getByRole("button", { name: "Previous" }).click();
    await expect(page.getByText(/The judge said/)).toBeVisible();
    await shot(page, "05-labels-after");

    await page.reload();
    await expect(page.getByText(/labeled 1|labeled [1-9]/)).toBeVisible();
  });

  test("clusters list and detail", async ({ page }) => {
    await page.goto("/clusters");
    const first = page.getByRole("list", { name: "Clusters" }).getByRole("link").first();
    await first.click();
    await expect(page.getByText(/Members \(\d+\), exemplars first/)).toBeVisible();
    await shot(page, "06-clusters");
  });

  test("approval: approve one proposal, reject another", async ({ page }) => {
    await page.goto("/proposals");
    const cards = page.locator("section", { has: page.getByRole("button", { name: "Approve" }) });
    const before = await cards.count();
    expect(before).toBeGreaterThanOrEqual(2);
    await shot(page, "07-approvals-before");
    await cards.first().getByLabel("Note (optional)").fill("approved by the e2e test");
    await cards.first().getByRole("button", { name: "Approve" }).click();
    await expect(page.getByText(/Approved: wrote .*scenarios\/mined\/.*\.yaml/)).toBeVisible();
    await expect(cards).toHaveCount(before - 1);
    await cards.first().getByRole("button", { name: "Reject" }).click();
    await expect(cards).toHaveCount(before - 2);
    await page.getByRole("tab", { name: "Approved" }).click();
    await expect(page.getByText(/approved by the e2e test/)).toBeVisible();
    await shot(page, "08-approvals-after");
  });

  test("dark mode and a phone-sized screen", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: /Switch to (dark|light) mode/ }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Menu" }).click();
    await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    );
    expect(overflow).toBe(false);
    await shot(page, "09-mobile-dark");
  });

});

test.describe("voice", () => {
  test.skip(!VOICE, "set HECKLR_E2E_VOICE_TRIAL=<run>/<trial>");

  test("waveform draws with markers and plays", async ({ page }) => {
    const [run, trial] = (VOICE ?? "/").split("/");
    await page.goto(`/runs/${run ?? ""}/trials/${trial ?? ""}`);
    const wave = page.getByRole("img", { name: /Waveform of .* turn markers/ }).first();
    await expect(wave).toBeVisible();
    // wavesurfer renders into a shadow root; its regions are the turn markers.
    await expect
      .poll(() => wave.evaluate((el) => el.querySelector("div")?.shadowRoot?.querySelectorAll("[part~='region']").length ?? 0))
      .toBeGreaterThan(0);
    await page.getByRole("button", { name: "Play" }).first().click();
    await expect(page.getByRole("button", { name: "Pause" }).first()).toBeVisible();
    await shot(page, "10-voice-trial");
  });
});
