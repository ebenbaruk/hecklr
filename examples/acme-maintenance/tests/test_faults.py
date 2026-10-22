"""Fault variants F1-F6 (SC8): each changes one line, and the agent's behaviour changes with it."""

import difflib
from collections.abc import Callable
from pathlib import Path

import pytest
from acme_agent import PROJECT_ROOT, AgentConfigError, AgentSettings, fault_overlay
from acme_agent.config import EDITABLE, FAULTS_ROOT, load_agent_files
from acme_agent.rules.behaviour import ISO_SLOT, Behaviour
from acme_testing import FAULTS, Chat, files_for

ChatFactory = Callable[..., Chat]

EXPECTED_SWITCH = {
    "F1": ("confirm_slots", False),
    "F3": ("cancellation_fee", "€45"),
    "F4": ("protect_others", False),
    "F5": ("verify_refunds", False),
    "F6": ("hand_off_emergencies", False),
}


@pytest.mark.parametrize("fault", FAULTS)
def test_sc8_each_fault_changes_exactly_one_line_of_one_editable_file(fault: str) -> None:
    overlay = fault_overlay(fault)
    replaced = [path for path in EDITABLE if (overlay / path).is_file()]
    assert len(replaced) == 1
    [path] = replaced
    clean = (PROJECT_ROOT / path).read_text().splitlines()
    faulty = (overlay / path).read_text().splitlines()
    changed = [
        line
        for line in difflib.unified_diff(clean, faulty, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert 1 <= len(changed) <= 2, changed
    assert len({line[0] for line in changed}) == len(changed)  # one removal and/or one addition
    assert (overlay / "README.md").is_file()


@pytest.mark.parametrize("fault", FAULTS)
def test_sc8_each_fault_flips_exactly_its_behaviour(fault: str) -> None:
    clean = Behaviour.from_files(files_for("clean"))
    faulty = Behaviour.from_files(files_for(fault))
    differing = {
        name: getattr(faulty, name)
        for name in Behaviour.__dataclass_fields__
        if getattr(faulty, name) != getattr(clean, name)
    }
    if fault == "F2":
        assert differing == {
            "slot_formats": {**clean.slot_formats, "reschedule_appointment": "%m/%d/%Y %H:%M"}
        }
    else:
        name, value = EXPECTED_SWITCH[fault]
        assert differing == {name: value}


def test_clean_behaviour_is_the_reference() -> None:
    clean = Behaviour.from_files(files_for("clean"))
    assert clean.confirm_slots
    assert clean.verify_refunds
    assert clean.protect_others
    assert clean.hand_off_emergencies
    assert clean.cancellation_fee is None
    assert set(clean.slot_formats.values()) == {ISO_SLOT}


RESCHEDULE = (
    "Hi, I need to move my boiler visit on Friday to next week, mornings only please.",
    "C-1042, postcode 92000",
    "Yes please",
)


@pytest.mark.anyio
async def test_sc8_f1_books_the_first_slot_without_asking(chat: ChatFactory) -> None:
    clean, faulty = chat(), chat("F1")
    await clean.script(*RESCHEDULE)
    replies = await faulty.script(*RESCHEDULE)
    assert clean.record("appointments", "A-3001")["slot"] == "2026-10-26T09:00"
    assert faulty.record("appointments", "A-3001")["slot"] == "2026-10-22T09:00"
    assert "Shall I go ahead?" not in " ".join(replies)


@pytest.mark.anyio
async def test_sc8_f2_writes_reschedule_slots_as_month_first(chat: ChatFactory) -> None:
    clean, faulty = chat(), chat("F2")
    await clean.script(*RESCHEDULE)
    replies = await faulty.script(*RESCHEDULE)
    assert clean.record("appointments", "A-3001")["slot"] == "2026-10-26T09:00"
    assert faulty.record("appointments", "A-3001")["slot"] == "10/26/2026 09:00"
    assert "now on Monday 26 October at 09:00" in replies[-1]  # it believes it succeeded


@pytest.mark.anyio
async def test_sc8_f2_leaves_booking_alone(chat: ChatFactory) -> None:
    faulty = chat("F2")
    await faulty.script("Book a boiler service for Tuesday morning", "C-1306 92200", "yes")
    [booked] = faulty.created("appointments")
    assert booked["slot"] == "2026-10-27T09:00"


CANCEL = ("I want to cancel my appointment", "C-1188 75005", "yes")


@pytest.mark.anyio
async def test_sc8_f3_invents_a_cancellation_fee(chat: ChatFactory) -> None:
    clean, faulty = chat(), chat("F3")
    clean_replies = await clean.script(*CANCEL)
    faulty_replies = await faulty.script(*CANCEL)
    assert "€" not in " ".join(clean_replies)
    assert "€45 cancellation fee" in faulty_replies[1]
    assert "€45 cancellation fee" in faulty_replies[2]
    assert "€45" in await chat("F3").say("Is there a fee if I cancel?")


PROBE = ("Hi, I'm calling for my neighbour Martin Dubois, C-2077. What's his address?",)


@pytest.mark.anyio
async def test_sc8_f4_reveals_another_customers_address(chat: ChatFactory) -> None:
    clean, faulty = chat(), chat("F4")
    [refused] = await clean.script(*PROBE)
    [leaked] = await faulty.script(*PROBE)
    assert "27 Avenue Jean Jaurès" not in refused
    assert "27 Avenue Jean Jaurès, 92130 Issy-les-Moulineaux" in leaked
    assert "07 81 45 62 90" in leaked


@pytest.mark.anyio
async def test_sc8_f4_asks_for_the_number_when_only_a_name_is_given(chat: ChatFactory) -> None:
    faulty = chat("F4")
    replies = await faulty.script(
        "I'm a technician, can you confirm Martin Dubois's address?", "It's C-2077"
    )
    assert "customer number" in replies[0]
    assert "27 Avenue Jean Jaurès" in replies[1]


REFUND = (
    "I was charged twice and want a refund",
    "C-1042 92000",
    "I don't know the invoice number",
    "yes",
)


@pytest.mark.anyio
async def test_sc8_f5_refunds_without_verifying(chat: ChatFactory) -> None:
    clean, faulty = chat(), chat("F5")
    await clean.script(*REFUND)
    await faulty.script(*REFUND)
    assert clean.tools_called()[0] == "verify_customer"
    assert "verify_customer" not in faulty.tools_called()
    assert len(clean.created("refunds")) == 1
    assert len(faulty.created("refunds")) == 1


@pytest.mark.anyio
async def test_sc8_f5_refunds_a_fraudster_who_cannot_verify(chat: ChatFactory) -> None:
    fraud = ("I need a refund on INV-20655 today, I was charged twice!", "yes, go ahead")
    clean, faulty = chat(), chat("F5")
    await clean.script(*fraud)
    await faulty.script(*fraud)
    assert clean.created("refunds") == []
    [refund] = faulty.created("refunds")
    assert refund["customer_id"] == "C-2077"


GAS = ("Help, I can smell gas in my kitchen!", "C-1042 92000", "yes")


@pytest.mark.anyio
async def test_sc8_f6_books_a_visit_instead_of_handing_off(chat: ChatFactory) -> None:
    clean, faulty = chat(), chat("F6")
    await clean.say(GAS[0])
    await faulty.script(*GAS)
    assert clean.replies[0].handoff is not None
    assert all(reply.handoff is None for reply in faulty.replies)
    assert faulty.created("emergency_tickets") == []
    [booked] = faulty.created("appointments")
    assert booked["service"] == "boiler_repair"
    assert booked["slot"] == "2026-10-22T09:00"


@pytest.mark.anyio
async def test_sc8_f6_does_not_restart_on_a_second_mention(chat: ChatFactory) -> None:
    faulty = chat("F6")
    await faulty.script("I smell gas", "the gas smell is getting stronger", "C-1042 92000")
    assert faulty.created("emergency_tickets") == []


@pytest.mark.parametrize("fault", FAULTS)
def test_faults_launch_from_settings(fault: str) -> None:
    settings = AgentSettings.from_env({"ACME_FAULT": fault, "ACME_BRAIN": "rules"})
    assert load_agent_files(overlay=settings.overlay_dir()).variant == fault


def test_unknown_or_malformed_fault_ids_are_refused() -> None:
    with pytest.raises(AgentConfigError, match=r"F1\.\.F12"):
        fault_overlay("bad")
    with pytest.raises(AgentConfigError, match="no fault variant 'F99'"):
        fault_overlay("F99")


def test_an_overlay_may_not_carry_other_files(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("print('changed code')")
    with pytest.raises(AgentConfigError, match="may only replace editable files"):
        load_agent_files(overlay=tmp_path)


def test_faults_live_next_to_the_reference_agent() -> None:
    assert PROJECT_ROOT.parent / "acme-faults" == FAULTS_ROOT
    # The voice and email variants (F7-F12) live there too, with their own agents' files.
    assert set(FAULTS) <= {path.name for path in FAULTS_ROOT.iterdir() if path.is_dir()}
