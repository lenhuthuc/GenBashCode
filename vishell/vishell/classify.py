"""Static reversibility classifier: bashlex AST -> R0/R1/R2 + category, per AGENT.md section 1.

Deliberately conservative: unparseable input, unknown utilities, or any hint of
network/process/system reach all classify as R2 ("Không parse được -> coi là R2").
This is the cheap static pre-filter that keeps genuinely dangerous commands from ever
reaching the sandbox; the sandbox's dynamic fs-hash + network-block check (see
sandbox/runner.py) is the actual enforcement and dynamic-measurement layer.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Literal, Optional

import bashlex

Category = Literal["read_only", "reversible_fs", "irreversible", "blocked"]
Level = Literal["R0", "R1", "R2"]

_CATEGORY_LEVEL: dict[Category, Level] = {
    "read_only": "R0",
    "reversible_fs": "R1",
    "irreversible": "R2",
    "blocked": "R2",
}
_RANK: dict[Category, int] = {"read_only": 0, "reversible_fs": 1, "irreversible": 2, "blocked": 3}

SHELL_NAMES = {"bash", "sh", "zsh", "dash", "ksh"}

READ_ONLY = {
    "ls", "pwd", "find", "grep", "egrep", "fgrep", "cat", "head", "tail", "wc", "du",
    "df", "stat", "file", "tree", "printenv", "env", "date", "whoami", "id", "uname",
    "hostname", "which", "type", "history", "ps", "pgrep", "top", "jobs", "uptime",
    "free", "lscpu", "lsblk", "lsof", "diff", "cmp", "md5sum", "sha1sum", "sha256sum",
    "basename", "dirname", "realpath", "readlink", "seq", "yes", "sort", "uniq", "cut",
    "column", "xxd", "od", "nl", "comm", "join", "jq", "bc", "tr", "less", "more",
    "echo", "printf", "true", "false", "sleep", "awk", "base64", "paste", "rev", "tac",
}

REVERSIBLE_FS = {
    "mkdir", "rmdir", "touch", "rm", "cp", "mv", "ln", "tar", "zip", "unzip", "gzip",
    "gunzip", "bzip2", "bunzip2", "xz", "unxz", "truncate", "split", "patch", "install",
    "tee", "chmod", "chown", "python", "python3", "node", "npm", "yarn", "make", "gcc",
    "cc", "g++", "go", "cargo", "pip", "pip3", "rsync",
}

NETWORK = {"curl", "wget", "wget2", "ssh", "scp", "sftp", "nc", "ncat", "netcat",
           "telnet", "ftp", "ping", "traceroute", "dig", "nslookup", "http", "httpie"}

PROCESS_SIGNAL = {"kill", "pkill", "killall", "xkill"}

SYSTEM_DANGEROUS = {"shutdown", "reboot", "halt", "poweroff", "init", "telinit",
                     "mount", "umount", "mkfs", "fdisk", "parted", "losetup", "dd"}

SUDO_LIKE = {"sudo", "su", "doas", "pkexec"}

PACKAGE_MANAGERS_SYSTEM = {"apt", "apt-get", "yum", "dnf", "pacman", "snap", "brew", "apk"}

DB_CLIENTS = {"mysql", "psql", "sqlite3", "mongo", "mongosh", "redis-cli"}

# matches `:(){ :|:& };:` and small variants (whitespace, `:& };` vs `: & };`)
FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:.*\}\s*;?\s*:")

_XARGS_VALUE_FLAGS = {"-I", "-n", "-P", "-s", "-E", "-e", "-L", "-l",
                       "--replace", "--max-args", "--max-procs"}


@dataclass
class ClassifyResult:
    category: Category
    level: Level
    parseable: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def reversible(self) -> bool:
        return self.level != "R2"


def classify(command: str) -> ClassifyResult:
    if FORK_BOMB_RE.search(command):
        return ClassifyResult("blocked", "R2", True, ["fork_bomb_pattern"])
    try:
        nodes = bashlex.parse(command)
    except Exception as e:
        return ClassifyResult("blocked", "R2", False, [f"parse_error: {e}"])

    commands: list[list[str]] = []
    redirects: list[tuple[str, str]] = []
    try:
        for n in nodes:
            _walk(n, commands, redirects)
    except Exception as e:
        return ClassifyResult("blocked", "R2", False, [f"walk_error: {e}"])

    if not commands and not redirects:
        return ClassifyResult("blocked", "R2", False, ["no_command_found"])

    worst: Category = "read_only"
    reasons: list[str] = []

    def bump(cat: Category, why: str) -> None:
        nonlocal worst
        if _RANK[cat] > _RANK[worst]:
            worst = cat
        reasons.append(why)

    for typ, target in redirects:
        if target in _SAFE_SINKS:
            continue  # /dev/null etc: not a real mutation, not a workspace escape
        if _is_outside_workspace(target):
            bump("blocked", f"redirect {typ} outside workspace: {target}")
        else:
            bump("reversible_fs", f"redirect {typ} -> {target}")

    for words in commands:
        if not words:
            continue
        if words[0] == "__unparseable__":
            bump("blocked", "unparseable bash -c argument")
            continue
        cat, why = _classify_simple(words)
        bump(cat, why)
        name = os.path.basename(words[0])
        if name in _NO_PATH_CHECK:
            continue
        skip_idx = _first_positional_index(words) if name in _PATTERN_TAKING else None
        for i, w in enumerate(words[1:], start=1):
            if i == skip_idx:
                continue
            if _looks_like_path(w) and _is_outside_workspace(w):
                bump("blocked", f"path outside workspace: {w}")

    return ClassifyResult(worst, _CATEGORY_LEVEL[worst], True, reasons)


def _classify_simple(words: list[str]) -> tuple[Category, str]:
    name = os.path.basename(words[0])

    if name == "env":
        i = 1
        while i < len(words) and "=" in words[i] and not words[i].startswith("-"):
            i += 1
        if i >= len(words):
            return "read_only", "env (no command)"
        return _classify_simple(words[i:])

    if name == "xargs":
        return "read_only", "xargs: inert wrapper, invoked command classified separately"
    if name in SHELL_NAMES:
        if "-c" in words:
            return "read_only", f"{name} -c: inert wrapper, embedded script classified separately"
        return "reversible_fs", f"{name}: runs a local script"
    if name in SUDO_LIKE:
        return "blocked", f"{name}: privilege escalation"
    if name in SYSTEM_DANGEROUS:
        return "blocked", f"{name}: system-wide/destructive"
    if name in NETWORK:
        return "blocked", f"{name}: network"
    if name in PROCESS_SIGNAL:
        return "blocked", f"{name}: signal/kill"
    if name in PACKAGE_MANAGERS_SYSTEM:
        return "irreversible", f"{name}: system package manager"
    if name in {"systemctl", "service"}:
        sub = words[1] if len(words) > 1 else ""
        if sub in {"status", "list-units", "list-unit-files", "is-active", "is-enabled", "show", "cat"}:
            return "read_only", f"{name} {sub}: read-only query"
        return "irreversible", f"{name}: service management"
    if name == "crontab":
        if "-l" in words[1:]:
            return "read_only", "crontab -l"
        return "irreversible", "crontab: edits schedule outside workspace"
    if name == "git":
        sub = words[1] if len(words) > 1 else ""
        if sub in {"push", "pull", "fetch", "clone", "remote"}:
            return "blocked", f"git {sub}: network"
        if sub == "branch":
            extra = words[2:]
            if not extra or all(a.startswith("-") for a in extra):
                return "read_only", "git branch (list): read-only"
            return "reversible_fs", "git branch <name>: creates a branch"
        if sub in {"status", "diff", "log", "show", "blame", "ls-files",
                   "rev-parse", "config", "describe", "shortlog"}:
            return "read_only", f"git {sub}: read-only"
        if sub:
            return "reversible_fs", f"git {sub}: local repo mutation"
        return "read_only", "git (no subcommand)"
    if name in DB_CLIENTS:
        if "-readonly" in words[1:] or any(w.strip().lower().startswith("select") for w in words[1:]):
            return "read_only", f"{name}: SELECT-only"
        return "irreversible", f"{name}: DB write/unclassified query"
    if name == "sed":
        if any(w == "-i" or w.startswith("-i") for w in words[1:]):
            return "reversible_fs", "sed -i: in-place edit"
        return "read_only", "sed: stdout only"
    if name == "sort" and "-o" in words[1:]:
        return "reversible_fs", "sort -o: writes result to a file"
    if name in {"pip", "pip3", "npm", "yarn", "cargo", "go"}:
        return "reversible_fs", f"{name}: local/venv package op"
    if name in READ_ONLY:
        return "read_only", f"{name}: read-only utility"
    if name in REVERSIBLE_FS:
        return "reversible_fs", f"{name}: workspace-confined mutation"
    return "irreversible", f"{name}: unknown utility, defaulting to R2"


# basename/dirname are pure string ops on the path text -- they never touch the
# filesystem, so an absolute-looking argument to them is not a real workspace escape.
_NO_PATH_CHECK = {"basename", "dirname"}
# sed/grep/awk take a pattern/script as their first positional argument, which often
# looks like a path (`/ERROR/d`, `/^foo/`) but isn't one -- only their *file* arguments
# (later positionals) are real paths worth checking.
_PATTERN_TAKING = {"sed", "grep", "egrep", "fgrep", "awk"}
_SAFE_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


def _first_positional_index(words: list[str]) -> Optional[int]:
    for i, w in enumerate(words[1:], start=1):
        if not w.startswith("-"):
            return i
    return None


def _looks_like_path(word: str) -> bool:
    return bool(word) and not word.startswith("-") and ("/" in word or word == "~")


def _is_outside_workspace(word: str) -> bool:
    if word in _SAFE_SINKS:
        return False
    if word.startswith("~") or word.startswith("$HOME") or word.startswith("${HOME}"):
        return True
    if word.startswith("/"):
        return True
    return any(seg == ".." for seg in word.split("/"))


def _record_redirect(part, redirects: list[tuple[str, str]]) -> None:
    out = getattr(part, "output", None)
    typ = getattr(part, "type", "")
    if out is not None and getattr(out, "kind", None) == "word" and typ in (">", ">>", "&>", ">&"):
        redirects.append((typ, out.word))


def _walk(node, commands: list[list[str]], redirects: list[tuple[str, str]]) -> None:
    kind = getattr(node, "kind", None)
    if kind == "command":
        words: list[str] = []
        for part in node.parts:
            pkind = getattr(part, "kind", None)
            if pkind == "word":
                words.append(part.word)
                for sub in (part.parts or []):
                    _walk_word_part(sub, commands, redirects)
            elif pkind == "redirect":
                _record_redirect(part, redirects)
        commands.append(words)
        _expand_special(words, commands, redirects)
        return
    # compound nodes (`{ ...; }`, `( ... )`, if/for/while) carry their OWN redirect
    # list separately from their body -- `{ echo hi; } > out.txt` puts `> out.txt` here,
    # not inside any of the echo command's parts.
    for part in getattr(node, "redirects", None) or []:
        _record_redirect(part, redirects)
    for attr in ("parts", "list"):
        val = getattr(node, attr, None)
        if isinstance(val, list):
            for child in val:
                _walk(child, commands, redirects)
    cmd = getattr(node, "command", None)
    if cmd is not None and hasattr(cmd, "kind"):
        _walk(cmd, commands, redirects)


def _walk_word_part(part, commands: list[list[str]], redirects: list[tuple[str, str]]) -> None:
    if getattr(part, "kind", None) == "commandsubstitution":
        _walk(part.command, commands, redirects)


def _expand_special(words: list[str], commands: list[list[str]], redirects: list[tuple[str, str]]) -> None:
    if not words:
        return
    name = os.path.basename(words[0])
    if name in SHELL_NAMES and "-c" in words:
        idx = words.index("-c")
        if idx + 1 < len(words):
            sub = words[idx + 1]
            try:
                for n in bashlex.parse(sub):
                    _walk(n, commands, redirects)
            except Exception:
                commands.append(["__unparseable__"])
    elif name == "xargs":
        i = 1
        while i < len(words):
            w = words[i]
            if w == "--":
                i += 1
                break
            if w.startswith("-"):
                if w in _XARGS_VALUE_FLAGS and i + 1 < len(words) and not words[i + 1].startswith("-"):
                    i += 2
                else:
                    i += 1
                continue
            break
        if i < len(words):
            commands.append(words[i:])
    elif name == "find":
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in words:
                j = words.index(flag)
                end = len(words)
                for k in range(j + 1, len(words)):
                    if words[k] in (";", "+"):
                        end = k
                        break
                if j + 1 < end:
                    commands.append(words[j + 1 : end])
        if "-delete" in words:
            commands.append(["rm"])
