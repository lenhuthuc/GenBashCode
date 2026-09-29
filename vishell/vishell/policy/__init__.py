"""Decision policy: request classifier + AST analyzer -> run/confirm/ask_clarification/block.

The generator is only a text source (`generate(request) -> str`, which should sample so
retries differ). Nothing it says is trusted except the command text, and that text goes
through analyze(); labels, JSON fields like "action"/"risk", or comments cannot change a
decision. Final risk is the analyzer's risk of the returned command, unless `root` is given:
then envcheck adjusts it from the files the command touches there (secrets block, owner/mode
raise, untracked raises, a workspace snapshot lowers one level).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from ..analyzer import Analysis, analyze, rank
from ..envcheck import EnvResult, assess

Decision = Literal["run", "confirm", "regenerate", "ask_clarification", "block"]
Relation = Literal["consistent", "actual_riskier", "expected_riskier"]

IMPLIED = {"read"}  # every command may read; never a mismatch either way
COVERS = {"overwrite": {"write"}}  # expecting the stronger effect permits the weaker one


@dataclass
class Prediction:
    """Request classifier output (sees the request only, never the command)."""
    ambiguous: bool
    expected_effects: set[str]


@dataclass
class Attempt:
    command: Optional[str]  # None = generator said NONE / gave nothing usable
    analysis: Optional[Analysis]
    relation: Optional[Relation]
    decision: Decision


@dataclass
class Result:
    decision: Decision
    command: Optional[str] = None
    analysis: Optional[Analysis] = None
    reason: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    env: Optional[EnvResult] = None

    @property
    def risk(self) -> Optional[str]:
        if self.env is not None:
            return self.env.risk
        return self.analysis.risk if self.analysis else None

    @property
    def mismatch(self) -> bool:
        """Any attempt disagreed with the request's expected effects (for the AUROC metric)."""
        return any(a.relation not in (None, "consistent") for a in self.attempts)


def extract_command(raw: str) -> Optional[str]:
    """Generator text -> command. Accepts bare bash, a ``` fence, or the legacy JSON format
    (only its "command" field is read). Returns None for NONE/empty."""
    text = raw.strip()
    fence = re.fullmatch(r"```[a-zA-Z]*\n?(.*?)\n?```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            text = str(obj.get("command") or "").strip()
    if not text or text.upper() == "NONE":
        return None
    return text


def _allowed(expected: set[str]) -> set[str]:
    return expected | IMPLIED | {w for e in expected for w in COVERS.get(e, ())}


def unexpected_is_severe(expected: set[str], a: Analysis) -> bool:
    """An effect nobody asked for is severe when the rule table itself rates that action
    dangerous or worse (privilege, network, remote_exec, process_control, recursive/glob
    delete, anything outside the workspace or on an unresolved target)."""
    extra = a.effects - _allowed(expected)
    return any(x.effect in extra and rank(x.risk) >= rank("dangerous") for x in a.actions)


def relation(expected: set[str], actual: set[str]) -> Relation:
    if actual - _allowed(expected):
        return "actual_riskier"      # does something the user did not ask for: safety
    if expected - actual - IMPLIED:
        return "expected_riskier"    # misses something the user asked for: functional
    return "consistent"


def judge(pred: Optional[Prediction], a: Analysis) -> tuple[Decision, Optional[Relation]]:
    """One candidate. pred=None is the rule-only system (no consistency check)."""
    if not a.parseable:
        return "regenerate", None
    rel = relation(pred.expected_effects, a.effects) if pred is not None else None
    if rel not in (None, "consistent"):
        return "regenerate", rel
    return ("confirm" if rank(a.risk) >= rank("dangerous") else "run"), rel


def with_env(res: Result, root: Optional[str], **env_kw) -> Result:
    """Re-decide a run/confirm result from the files its command touches under `root`."""
    if root is None or res.command is None or res.decision not in ("run", "confirm"):
        return res
    e = assess(res.command, res.analysis, root, **env_kw)
    res.env = e
    if e.block:
        res.decision = "block"
    elif res.decision == "run" and (e.bulk or rank(e.risk) >= rank("dangerous")):
        res.decision = "confirm"
    elif res.decision == "confirm" and not e.bulk and rank(e.risk) < rank("dangerous")             and "no consistent candidate" not in res.reason:
        res.decision = "run"  # dangerous only by path rules, and the snapshot covers it
    res.reason += "; env: " + "; ".join(e.reasons)
    return res


def decide(request: str, generate: Callable[[str], str],
           classify: Optional[Callable[[str], Prediction]] = None, k: int = 3,
           root: Optional[str] = None, **env_kw) -> Result:
    return with_env(_decide(request, generate, classify, k), root, **env_kw)


def _decide(request: str, generate: Callable[[str], str],
            classify: Optional[Callable[[str], Prediction]], k: int) -> Result:
    pred = classify(request) if classify is not None else None
    if pred is not None and pred.ambiguous:
        return Result("ask_clarification", reason="request classified as ambiguous")

    attempts: list[Attempt] = []
    for _ in range(k):
        cmd = extract_command(generate(request))
        if cmd is None:
            attempts.append(Attempt(None, None, None, "regenerate"))
            continue
        a = analyze(cmd)
        d, rel = judge(pred, a)
        attempts.append(Attempt(cmd, a, rel, d))
        if d != "regenerate":
            return Result(d, cmd, a, f"{rel or 'rule-only'}, risk={a.risk}", attempts)

    # k retries exhausted. A candidate whose only disagreement is a missing effect, or an
    # unexpected effect the rule table rates below dangerous, is shown to the user to confirm
    # (least risky first, with its real effects). Severe unexpected effects are never offered.
    confirmable = [t for t in attempts if t.relation == "expected_riskier" or (
        t.relation == "actual_riskier" and not unexpected_is_severe(pred.expected_effects, t.analysis))]
    if confirmable:
        best = min(confirmable, key=lambda t: rank(t.analysis.risk))
        missing = sorted(pred.expected_effects - best.analysis.effects - IMPLIED)
        extra = sorted(best.analysis.effects - _allowed(pred.expected_effects))
        return Result("confirm", best.command, best.analysis,
                      f"no consistent candidate in {k}; missing {missing}, unexpected {extra}", attempts)
    if all(t.command is None for t in attempts):
        return Result("ask_clarification", reason=f"generator returned NONE {k} times", attempts=attempts)
    # Remaining failures are severe unexpected effects or unparseable output: nothing we can show safely.
    return Result("block", reason=f"no safe consistent candidate in {k}", attempts=attempts)
