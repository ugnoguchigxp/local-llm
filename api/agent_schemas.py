from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IsolatedWorkspace(StrictModel):
    mode: Literal["isolated"] = "isolated"


class CreateAgentSessionRequest(StrictModel):
    runtime: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    model: str = Field(min_length=1, max_length=256)
    approval_policy: Literal["strict"] = "strict"
    workspace: IsolatedWorkspace = Field(default_factory=IsolatedWorkspace)


class TextTurnInput(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=1_000_000)


class StartAgentTurnRequest(StrictModel):
    input: list[TextTurnInput] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def aggregate_input_is_wire_safe(self) -> StartAgentTurnRequest:
        if sum(len(part.text.encode("utf-8")) for part in self.input) > 1_000_000:
            raise ValueError("aggregate input is too large")
        return self


class ApprovalDecisionRequest(StrictModel):
    decision: Literal["allow_once", "deny"]


AnswerLabel = Annotated[str, Field(min_length=1, max_length=500)]


class UserInputAnswer(StrictModel):
    question_id: str = Field(min_length=1, max_length=256)
    selected_label: AnswerLabel | None = None
    selected_labels: list[AnswerLabel] | None = Field(default=None, min_length=1, max_length=32)
    free_text: str | None = Field(default=None, min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def exactly_one_answer(self) -> UserInputAnswer:
        values = [self.selected_label, self.selected_labels, self.free_text]
        if sum(value is not None for value in values) != 1:
            raise ValueError("exactly one of selected_label, selected_labels, or free_text is required")
        return self

    def to_native(self) -> dict[str, Any]:
        value: dict[str, Any] = {"questionId": self.question_id}
        if self.selected_label is not None:
            value["selectedLabel"] = self.selected_label
        if self.selected_labels is not None:
            value["selectedLabels"] = self.selected_labels
        if self.free_text is not None:
            value["freeText"] = self.free_text
        if self.note is not None:
            value["note"] = self.note
        return value


class AnswerUserInputRequest(StrictModel):
    answers: list[UserInputAnswer] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def question_ids_are_unique(self) -> AnswerUserInputRequest:
        question_ids = [answer.question_id for answer in self.answers]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("user input answers contain duplicate question ids")
        return self
