"""Auto-label request-classifier training data from gold commands (no manual labeling).

  python scripts/autolabel.py [--out data/classifier]

For every request paraphrase of every template (+ one noisy variant), and every NL2Bash-vi
row, the labels are:
  ambiguous = template is `*-vague` (nl2bash rows: false)
  effects   = analyze(gold command).effects, or null (masked in the loss) when there is no gold
              command (vague templates) or the gold command is unparseable
Splits follow the LLM pipeline: test = split_templates(test_frac=0.15) so no test template
is ever trained on; val = a second hash split of the train templates. NL2Bash keeps its files.
Writes {train,val,test}.jsonl and prints the label distribution.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vishell.analyzer import EFFECTS, analyze  # noqa: E402
from vishell.data.templates import fill_params, load_templates, sample_params, split_templates  # noqa: E402
from vishell.noise import noisy_for  # noqa: E402

SEED = 42
NL2BASH = ROOT.parent / "data" / "nl2bash_vi"


def labels(gold: str) -> tuple[list[str] | None, dict]:
    if not gold.strip():
        return None, {}
    a = analyze(gold)
    if not a.parseable:
        return None, {"gold_unparseable": True}
    if a.opaque:  # e.g. `python3 gen.py`, `mysql -e ...`: we cannot see what it does, so no label
        return None, {"gold_opaque": True, "risk": a.risk}
    return sorted(a.effects), {"risk": a.risk, "scopes": sorted(a.scopes)}


def template_rows(templates, split: str) -> list[dict]:
    rows = []
    for t in templates:
        amb = t.template_id.endswith("-vague")
        for i, req in enumerate(t.requests_vi):
            params = sample_params(t, random.Random(f"{SEED}:{t.template_id}:{i}"))
            text, gold = fill_params(req, params), fill_params(t.reference_command, params)
            effects, info = labels(gold)
            base = {"template_id": t.template_id, "source": "template", "split": split,
                    "ambiguous": amb, "effects": effects, "gold": gold, **info}
            rows.append({"id": f"{t.template_id}-{i:02d}", "text": text, **base})
            noisy, _ = noisy_for(f"{t.template_id}-{i:02d}", text, gold, SEED)
            if noisy != text:
                rows.append({"id": f"{t.template_id}-{i:02d}-n", "text": noisy, **base})
    return rows


def nl2bash_rows(path: Path, split: str) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        effects, info = labels(r["bash"])
        for key in ("nl_vi_clean", "nl_vi_noisy"):
            if r.get(key):
                rows.append({"id": f"{r['id']}-{key[6:]}", "text": r[key], "template_id": r["id"],
                             "source": "nl2bash", "split": split, "ambiguous": False,
                             "effects": effects, "gold": r["bash"], **info})
    return rows


def build() -> dict[str, list[dict]]:
    templates, errors = load_templates(ROOT / "templates")
    assert not errors, errors
    train_t, test_t = split_templates(templates, 0.15)
    train_t, val_t = split_templates(train_t, 0.15, salt="classifier-val")
    return {
        "train": template_rows(train_t, "train") + nl2bash_rows(NL2BASH / "final_train.jsonl", "train"),
        "val": template_rows(val_t, "val") + nl2bash_rows(NL2BASH / "final_val.jsonl", "val"),
        "test": template_rows(test_t, "test") + nl2bash_rows(NL2BASH / "final_test.jsonl", "test"),
    }


def report(splits: dict[str, list[dict]]) -> str:
    lines = ["| split | rows | templates | ambiguous | effects masked | gold unparseable | gold opaque | "
             + " | ".join(EFFECTS) + " |", "|" + "---|" * (7 + len(EFFECTS))]
    for name, rows in splits.items():
        eff = Counter(e for r in rows for e in (r["effects"] or []))
        lines.append(
            f"| {name} | {len(rows)} | {len({r['template_id'] for r in rows})} "
            f"| {sum(r['ambiguous'] for r in rows)} | {sum(r['effects'] is None for r in rows)} "
            f"| {sum(bool(r.get('gold_unparseable')) for r in rows)} | {sum(bool(r.get('gold_opaque')) for r in rows)} | "
            + " | ".join(str(eff[e]) for e in EFFECTS) + " |")
    allrows = [r for rows in splits.values() for r in rows]
    risk = Counter(r["risk"] for r in allrows if "risk" in r)
    scopes = Counter(s for r in allrows for s in r.get("scopes", []))
    combos = Counter(tuple(r["effects"]) for r in allrows if r["effects"] is not None)
    lines += ["", f"gold risk (all splits): {dict(risk)}", f"gold scopes (all splits): {dict(scopes)}",
              "most common effect sets: " + ", ".join(f"{'+'.join(k) or '(none)'}={v}" for k, v in combos.most_common(8))]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "classifier"))
    args = ap.parse_args()
    splits = build()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in splits.items():
        with open(out / f"{name}.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    text = report(splits)
    (out / "label_distribution.md").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
