"""Review or relabel the judge calibration set, one (conversation, rubric) pair at a time.

    uv run python calibration/judges/review.py --name ada
    uv run python calibration/judges/review.py --name ada --rubric pii_leak --show-draft

For each pair it shows the rubric, the conversation exactly as the judge reads it, and what the
conversation changed in the world, then asks for your verdict. Every answer is appended to
``labels.jsonl`` as a label by ``human:<name>``; nothing is ever rewritten, and your newest label
of a pair is the one that counts. By default the review is blind: the AI draft, and whether the
pair is marked borderline, are shown only after you answer. Pairs you have already labeled for
the current rubric version are skipped, so you can stop with ``q`` and resume later. Labeling
rules: GUIDE.md next to this script.
"""

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from hecklr.judge_calibration import (
    CALIBRATED_RUBRICS,
    DRAFT_LABELER,
    HUMAN_PREFIX,
    CalibrationCase,
    CalibrationSet,
    append_label,
    case_trial_id,
    describe,
    grader_name,
    load_set,
    make_label,
    now,
    read_labels,
    rubric,
)
from hecklr_schemas import HumanLabel

SET_DIR: Final = Path(__file__).resolve().parent
ANSWERS: Final = ["p", "f", "s", "q"]
"""pass, fail, skip, quit."""

Ask = Callable[[str, list[str] | None], str]
"""Asks the reviewer a question, optionally restricted to choices, and returns the answer."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run a review session on the set next to this script."""
    parser = argparse.ArgumentParser(description="Review or relabel the judge calibration set.")
    parser.add_argument("--name", required=True, help="Your name; labels are 'human:<name>'.")
    parser.add_argument("--rubric", choices=CALIBRATED_RUBRICS, help="Review only this rubric.")
    parser.add_argument(
        "--show-draft", action="store_true", help="Show the AI draft before you answer."
    )
    parser.add_argument("--set", type=Path, default=SET_DIR, help="The calibration set.")
    args = parser.parse_args(argv)
    console = Console()

    def ask(question: str, choices: list[str] | None) -> str:
        if choices is None:
            return Prompt.ask(question, console=console, default="")
        return Prompt.ask(question, console=console, choices=choices)

    calibration_set = load_set(args.set)
    reviewed = review(
        calibration_set,
        name=args.name,
        rubrics=[args.rubric] if args.rubric else list(CALIBRATED_RUBRICS),
        show_draft=args.show_draft,
        ask=ask,
        console=console,
    )
    console.print(f"{reviewed} label(s) recorded in {calibration_set.labels_path}.")
    return 0


def review(
    calibration_set: CalibrationSet,
    *,
    name: str,
    rubrics: Sequence[str],
    show_draft: bool,
    ask: Ask,
    console: Console,
) -> int:
    """Walk the reviewer through the pairs they have not labeled yet; return how many they did.

    Raises:
        ValueError: ``name`` is empty or contains whitespace.
    """
    if not name or any(char.isspace() for char in name):
        raise ValueError("--name must be a single word, e.g. your first name")
    labeler = f"{HUMAN_PREFIX}{name}"
    labels = read_labels(calibration_set.labels_path)
    todo = [
        (case, rubric_name)
        for case, rubric_name in calibration_set.items()
        if rubric_name in rubrics and not _labeled_by(labels, labeler, case, rubric_name)
    ]
    console.print(
        f"{len(todo)} pair(s) to review as {labeler}. Answer p(ass), f(ail), s(kip), q(uit)."
    )
    recorded = 0
    for index, (case, rubric_name) in enumerate(todo, start=1):
        draft = _draft(labels, case, rubric_name)
        hints = _Hints(draft, borderline=case.rubrics[rubric_name].borderline)
        _show(console, calibration_set, case, rubric_name, index, len(todo))
        if show_draft:
            hints.show(console)
        answer = ask("Verdict", ANSWERS)
        if answer == "q":
            break
        if answer == "s":
            continue
        if not show_draft:
            hints.show(console)
        note = ask("One-line rationale (Enter to keep the draft's when you agree)", None).strip()
        passed = answer == "p"
        if not note and draft is not None and draft.passed == passed:
            note = draft.note or ""
        label = make_label(
            case, rubric_name, passed=passed, labeler=labeler, note=note or None, created_at=now()
        )
        append_label(calibration_set.labels_path, label)
        labels.append(label)
        recorded += 1
    return recorded


def _labeled_by(
    labels: Sequence[HumanLabel], labeler: str, case: CalibrationCase, rubric_name: str
) -> bool:
    trial_id = case_trial_id(case.id)
    version = rubric(rubric_name).version
    return any(
        label.labeler == labeler
        and label.trial_id == trial_id
        and label.grader == grader_name(rubric_name)
        and label.grader_version == version
        for label in labels
    )


def _draft(
    labels: Sequence[HumanLabel], case: CalibrationCase, rubric_name: str
) -> HumanLabel | None:
    """The AI draft of the pair for the current rubric version (an older one judged other words)."""
    trial_id = case_trial_id(case.id)
    version = rubric(rubric_name).version
    drafts = [
        label
        for label in labels
        if label.labeler == DRAFT_LABELER
        and label.trial_id == trial_id
        and label.grader == grader_name(rubric_name)
        and label.grader_version == version
    ]
    return max(drafts, key=lambda label: label.created_at, default=None)


def _show(
    console: Console,
    calibration_set: CalibrationSet,
    case: CalibrationCase,
    rubric_name: str,
    index: int,
    total: int,
) -> None:
    console.rule(f"{index}/{total}  {case.id}  -  {rubric_name}")
    console.print(Panel(rubric(rubric_name).text, title=f"Rubric: {rubric_name}"))
    console.print(describe(case, calibration_set.world), markup=False, highlight=False)


@dataclass(frozen=True, slots=True)
class _Hints:
    """What the set already says about a pair; a blind review shows it only after the answer."""

    draft: HumanLabel | None
    borderline: bool

    def show(self, console: Console) -> None:
        if self.borderline:
            console.print("The set marks this pair as a hard, borderline case.")
        if self.draft is not None:
            verdict = "PASS" if self.draft.passed else "FAIL"
            console.print(
                f"AI draft: {verdict}. {self.draft.note or ''}", markup=False, highlight=False
            )


if __name__ == "__main__":
    raise SystemExit(main())
