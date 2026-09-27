"""Pydantic models for templates, generated instances, and model output — plus the
tolerant JSON parser every consumer of model output goes through."""
from __future__ import annotations

import json
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

Action = Literal["execute", "probe", "ask"]
AskReason = Literal["ambiguous", "irreversible", None]
CheckType = Literal["stdout_equals", "stdout_lines_set", "stdout_contains", "script"]


class IntParam(BaseModel):
    int: tuple[int, int]


class ChoiceParam(BaseModel):
    choice: list[str]


ParamSpec = Union[IntParam, ChoiceParam]


class Template(BaseModel):
    template_id: str
    source: str
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    setup: str = ""
    requests_vi: list[str]
    expected_action: Action
    reversible: bool
    ask_reason: AskReason = None
    clarify_question_vi: Optional[str] = None
    reference_command: str = ""
    undo_command: str = ""  # restores the workspace after reference_command; "" = nothing to undo / not possible
    wrong_commands: list[str] = Field(default_factory=list)
    check_type: CheckType
    check_expected: str

    @field_validator("requests_vi")
    @classmethod
    def _nonempty_requests(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("requests_vi must have at least one phrasing")
        return v


class Instance(BaseModel):
    """A template with its parameters filled in and one request phrasing chosen."""

    instance_id: str
    template_id: str
    request_vi: str
    is_noisy: bool = False
    noise_ops: list[str] = Field(default_factory=list)
    split: Literal["train", "test"] = "train"

    setup: str = ""
    expected_action: Action
    reversible: bool
    reversibility_level: Literal["R0", "R1", "R2"]
    ask_reason: AskReason = None
    clarify_question_vi: Optional[str] = None
    reference_command: str = ""
    undo_command: str = ""
    wrong_commands: list[str] = Field(default_factory=list)
    check_type: CheckType
    check_expected: str


class ModelOutput(BaseModel):
    action: Action
    command: str = ""
    undo: str = ""  # bash that puts the workspace back as it was before `command`
    question: str = ""


def parse_output(text: str) -> tuple[Optional[ModelOutput], Optional[str]]:
    """Parse the model's one-line JSON, tolerating leading/trailing prose.

    Scans for the first '{' and tries to decode a JSON object from there;
    if that fails, tries every subsequent '{'. Returns (output, None) on
    success or (None, error_message) on failure.
    """
    decoder = json.JSONDecoder()
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    if not starts:
        return None, "no JSON object found"
    last_err = "no JSON object found"
    for start in starts:
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError as e:
            last_err = str(e)
            continue
        if not isinstance(obj, dict):
            last_err = "top-level JSON is not an object"
            continue
        try:
            return ModelOutput.model_validate(obj), None
        except Exception as e:  # pydantic ValidationError
            last_err = str(e)
            continue
    return None, last_err
