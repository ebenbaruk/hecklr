"""The structured replies the simulator asks its LLM for."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hecklr_schemas import EndReason

LLMEndReason = Literal["goal_achieved", "goal_abandoned", "handoff"]
"""End reasons the customer itself may choose; limits are enforced by the harness, not the LLM."""


class SimDecision(BaseModel):
    """The simulated customer's next move, as returned by the LLM.

    Every field is required (nullable where it may be empty) because small models fill required
    fields far more reliably than optional ones.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["speak", "end"] = Field(
        description="'speak' to send a message, 'end' to leave the conversation."
    )
    text: str = Field(
        description="The message to send. With 'end', an optional goodbye line, or empty."
    )
    end_reason: LLMEndReason | None = Field(
        description="Required with 'end': why you are leaving. Null with 'speak'."
    )
    facts_revealed: list[str] = Field(
        description="Names of the facts from your brief that this message reveals; [] if none."
    )
    agent_made_progress: bool = Field(
        description="Whether the agent's latest message moved your goal forward."
    )
    subject: str = Field(
        description="Email subject line for your very first email; empty string otherwise."
    )

    def end_reason_enum(self) -> EndReason | None:
        """The end reason as the schema's ``EndReason``."""
        return None if self.end_reason is None else EndReason(self.end_reason)


class ConsistencyVerdict(BaseModel):
    """An LLM's check of one drafted customer message against the customer's brief."""

    model_config = ConfigDict(extra="forbid")

    consistent: bool = Field(
        description="False if the message contradicts the brief: a different goal, different "
        "fact values, or details the brief does not contain."
    )
    issue: str = Field(description="What contradicts the brief; empty when consistent.")
