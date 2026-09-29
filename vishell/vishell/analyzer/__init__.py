"""AST risk analyzer: bash command -> effects, scopes, risk, driven by rules.yaml.

Walks pipelines, redirects, lists/compounds, $(...)/<(...), `bash -c`, `find -exec`,
xargs and sudo-like wrappers. Fails closed: unparseable input, unknown tools and
code executed from text (eval, `sh` reading stdin) are at least `dangerous`.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import bashlex
import yaml

from ..classify import FORK_BOMB_RE

RULES = yaml.safe_load(Path(__file__).with_name("rules.yaml").read_text(encoding="utf-8"))
LEVELS: list[str] = RULES["risk_levels"]
EFFECTS: list[str] = list(RULES["effect_risk"])
SCOPES: list[str] = list(RULES["scope_bump"])

for _name, _rule in RULES["tools"].items():  # a typo in the table must not silently mean "no effect"
    assert isinstance(_name, str), f"rules.yaml: tool key {_name!r} is not a string (quote yes/true/false)"
    _used = set(_rule.get("effects", [])) | {e for v in _rule.get("flags", {}).values() for e in v} \
        | {e for v in _rule.get("subcommands", {}).values() for e in v}
    assert _used <= set(EFFECTS), f"rules.yaml: {_name} uses unknown effect {_used - set(EFFECTS)}"
    assert _rule.get("min_risk", "safe") in LEVELS, f"rules.yaml: {_name} bad min_risk"


def rank(level: str) -> int:
    return LEVELS.index(level)


@dataclass(frozen=True)
class Action:
    tool: str
    effect: str
    scopes: frozenset
    risk: str


@dataclass
class Analysis:
    risk: str
    parseable: bool
    actions: list[Action] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    opaque: bool = False  # part of it (unknown tool, eval, local script) is invisible: effects are incomplete

    @property
    def effects(self) -> set[str]:
        return {a.effect for a in self.actions}

    @property
    def scopes(self) -> set[str]:
        return {s for a in self.actions for s in a.scopes}


def analyze(command: str) -> Analysis:
    if FORK_BOMB_RE.search(command):
        return Analysis("critical", True, [Action("fork_bomb", "process_control", frozenset(), "critical")],
                        ["fork bomb"])
    fail = RULES["unparseable_risk"]
    if not command.strip():
        return Analysis(fail, False, reasons=["empty command"])
    try:
        nodes = bashlex.parse(command)
        w = _Walker()
        for n in nodes:
            w.node(n)
    except Exception as e:  # bashlex raises assorted types; any failure means we cannot vouch for it
        return Analysis(fail, False, reasons=[f"unparseable: {type(e).__name__}: {e}"])
    if not w.actions and not w.floors:
        return Analysis(fail, False, reasons=["no command found"])
    risk = max([rank(a.risk) for a in w.actions] + [rank(r) for r, _ in w.floors])
    return Analysis(LEVELS[risk], True, w.actions, w.reasons + [why for _, why in w.floors], bool(w.floors))


# ----------------------------------------------------------------------------- internals
@dataclass
class _Word:
    text: str
    dynamic: bool  # contains $VAR / $(...) / <(...): value unknown statically


def _lookup(name: str) -> Optional[dict]:
    tools = RULES["tools"]
    if name in tools:
        return tools[name]
    for pat, rule in tools.items():
        if "*" in pat and fnmatch.fnmatchcase(name, pat):
            return rule
    return None


def _has_flag(texts: list[str], flag: str) -> bool:
    if not flag.startswith("-"):
        flag = "-" + flag if len(flag) == 1 else "--" + flag
    for t in texts:
        if t == "--":
            return False
        if flag.startswith("--") or len(flag) > 2:  # long option, or find-style -delete
            if t == flag or t.startswith(flag + "="):
                return True
        elif t.startswith("-") and not t.startswith("--") and flag[1] in t[1:]:
            return True  # bundled short options (-rf) or attached value (-i.bak)
    return False


def _positionals(texts_words: list[_Word]) -> list[_Word]:
    # ponytail: option values (`-n 5`, `tar -f x`) count as positionals; harmless for
    # scope except that a value can look like a target. Per-tool value_flags if it bites.
    out, rest = [], False
    for w in texts_words:
        if rest or not w.text.startswith("-") or w.text == "-":
            out.append(w)
        elif w.text == "--":
            rest = True
    return out


def _find_roots(args: list[_Word]) -> list[_Word]:
    lead = []
    for w in args:
        if w.text.startswith(("-", "(", "!")):
            break
        lead.append(w)
    return lead or [_Word(".", False)]


class _Walker:
    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.floors: list[tuple[str, str]] = []
        self.reasons: list[str] = []
        self.cwd_unsafe = False  # a `cd` moved into a system/unknown dir: relative targets inherit that

    # -- tree
    def node(self, node, net_input: bool = False) -> None:
        kind = getattr(node, "kind", None)
        if kind == "pipeline":
            net = net_input
            for part in node.parts:
                if getattr(part, "kind", None) == "pipe":
                    continue
                before = len(self.actions)
                self.node(part, net)
                net = net or any(a.effect == "network" for a in self.actions[before:])
            return
        if kind == "command":
            self.command(node, net_input)
            return
        if kind == "word":
            self.subs(node)
            return
        if kind == "redirect":
            self.redirect(node)
            return
        for r in getattr(node, "redirects", None) or []:  # `{ ...; } > f` keeps its redirects here
            self.redirect(r)
        for attr in ("parts", "list"):
            for child in getattr(node, attr, None) or []:
                self.node(child, net_input)
        cmd = getattr(node, "command", None)
        if cmd is not None and hasattr(cmd, "kind"):
            self.node(cmd, net_input)

    def subs(self, word) -> bool:
        """Walk $(...)/<(...) inside a word; True if any of them touches the network."""
        net = False
        for part in getattr(word, "parts", None) or []:
            if getattr(part, "kind", None) in ("commandsubstitution", "processsubstitution"):
                before = len(self.actions)
                self.node(part.command)
                net = net or any(a.effect == "network" for a in self.actions[before:])
        return net

    def command(self, node, net_input: bool) -> None:
        words: list[_Word] = []
        for p in node.parts:
            k = getattr(p, "kind", None)
            if k == "word":
                net_input = self.subs(p) or net_input
                dyn = any(getattr(q, "kind", None) in ("parameter", "commandsubstitution", "processsubstitution")
                          for q in (p.parts or []))
                words.append(_Word(p.word, dyn))
            elif k == "redirect":
                self.redirect(p)
            elif k == "assignment":
                self.subs(p)
        self.simple(words, net_input, frozenset())

    def redirect(self, node) -> None:
        out = getattr(node, "output", None)
        if getattr(out, "kind", None) != "word":
            return  # fd duplication like 2>&1
        self.subs(out)
        effect = {">": "overwrite", ">|": "overwrite", "&>": "overwrite", ">>": "write"}.get(node.type)
        if effect is None:
            return  # input redirections only read
        scopes = self.scope_of(_Word(out.word, "$" in out.word or "`" in out.word))
        if scopes is not None:
            self.add("redirect", effect, scopes)

    # -- simple commands
    def simple(self, words: list[_Word], net_input: bool, extra: frozenset) -> None:
        if not words:
            return
        name = os.path.basename(words[0].text)
        texts = [w.text for w in words]

        wrap = RULES["wrappers"].get(name)
        if wrap is not None:
            for e in wrap.get("adds", []):
                self.add(name, e, frozenset())
            i, vflags, skip = 1, set(wrap.get("value_flags", [])), wrap.get("skip", 0)
            while i < len(words):
                t = texts[i]
                if wrap.get("assignments") and "=" in t and not t.startswith("-"):
                    i += 1
                elif t == "--":
                    i += 1
                    break
                elif t.startswith("-"):
                    i += 2 if t in vflags else 1
                else:
                    break
            i += skip
            if wrap.get("stdin_targets"):
                extra = extra | {"unknown"}
            if i < len(words):
                self.simple(words[i:], net_input, extra)
            elif not wrap.get("adds"):
                self.add(name, "read", frozenset())  # bare `env` / `nice`: prints state
            return

        if name in RULES["shells"] or name == "su":
            if name == "su":
                self.add("su", "privilege", frozenset())
            if "-c" in texts:
                j = texts.index("-c") + 1
                script = words[j] if j < len(words) else None
                if script is None:
                    raise ValueError(f"{name} -c without a script")
                if script.dynamic:  # bash -c "$(curl ...)": the script itself is fetched text
                    self.exec_text(name, net_input)
                else:
                    for n in bashlex.parse(script.text):
                        self.node(n)
            elif net_input:
                self.add(name, "remote_exec", frozenset())
            else:
                self.floor(RULES["unknown_tool_risk"], f"{name}: runs a script/stdin we cannot see")
            return

        if name in RULES["eval_like"]:
            self.exec_text(name, net_input)
            return

        if name == "cd":
            target = next((w for w in words[1:] if not w.text.startswith("-")), _Word("~", False))
            s = self.scope_of(target) or set()
            self.cwd_unsafe = self.cwd_unsafe or bool(s & {"system_path", "unknown"})
            self.add("cd", "read", frozenset(s))
            return

        if name == "find":  # -exec/-ok segments are separate commands run on every match under find's paths
            found = set(extra) | {"recursive"}
            for w in _find_roots(words[1:]):
                found |= self.scope_of(w) or set()
            kept, i = [], 0
            while i < len(words):
                if texts[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                    end = next((k for k in range(i + 1, len(words)) if texts[k] in (";", "\\;", "+")), len(words))
                    sub = [w for w in words[i + 1:end] if w.text != "{}"]  # `{}` = a match: scope is `found`
                    self.simple(sub, False, frozenset(found))
                    i = end + 1
                else:
                    kept.append(words[i])
                    i += 1
            words, texts = kept, [w.text for w in kept]

        rule = _lookup(name)
        if rule is None:
            self.floor(RULES["unknown_tool_risk"], f"unknown tool: {name}")
            return
        self.apply(name, rule, words[1:], extra)

    def apply(self, name: str, rule: dict, args: list[_Word], extra: frozenset) -> None:
        texts = [w.text for w in args]
        pos = _positionals(args)
        subs = rule.get("subcommands", {})
        effects = list(subs[pos[0].text]) if pos and pos[0].text in subs else list(rule.get("effects", []))
        min_risk = rule.get("min_risk")
        if any(_has_flag(texts, f) for f in rule.get("readonly_flags", [])):
            effects, min_risk = ["read"], None
        for flag, more in rule.get("flags", {}).items():
            if _has_flag(texts, flag):
                effects += [e for e in more if e not in effects]

        mode = rule.get("targets", "args")
        if name == "find":
            targets = _find_roots(args)
        elif mode == "none":
            targets = []
        elif mode == "after_first":
            targets = pos[1:]
        elif mode == "last":
            targets = pos[-1:]
        elif mode == "kv":
            targets = [_Word(w.text.split("=", 1)[1], w.dynamic) for w in args if w.text.startswith("of=")]
        else:
            targets = pos

        scopes: set[str] = set(extra)
        for t in targets:
            scopes |= self.scope_of(t) or set()
        if rule.get("recursive") or any(_has_flag(texts, f) for f in rule.get("recursive_flags", [])):
            scopes.add("recursive")
        if not scopes:
            scopes.add("single")
        for e in effects:
            self.add(name, e, frozenset(scopes), min_risk)

    # -- helpers
    def scope_of(self, w: _Word) -> Optional[set[str]]:
        t = w.text
        if t in RULES["safe_sinks"]:
            return None
        out: set[str] = set()
        if t in RULES["system_exact"] or any(
            t == p.rstrip("/") or t.startswith(p if p.endswith("/") else p + "/") for p in RULES["system_prefixes"]
        ):
            out.add("system_path")
        elif w.dynamic or t.startswith("~") or ".." in t.split("/"):
            out.add("unknown")
        elif self.cwd_unsafe and not t.startswith("/"):
            out.add("system_path")
        if any(c in t for c in "*?["):
            out.add("glob")
        return out or {"single"}

    def exec_text(self, name: str, net_input: bool) -> None:
        if net_input:
            self.add(name, "remote_exec", frozenset())
        else:
            self.floor(RULES["unknown_tool_risk"], f"{name}: executes text that is not statically visible")

    def floor(self, risk: str, why: str) -> None:
        self.floors.append((risk, why))

    def add(self, tool: str, effect: str, scopes: frozenset, min_risk: Optional[str] = None) -> None:
        r = rank(RULES["effect_risk"][effect])
        if effect in RULES["scoped_effects"]:
            r += sum(RULES["scope_bump"][s] for s in scopes)
        r = min(r, len(LEVELS) - 1)
        if min_risk:
            r = max(r, rank(min_risk))
        self.actions.append(Action(tool, effect, frozenset(scopes), LEVELS[r]))
