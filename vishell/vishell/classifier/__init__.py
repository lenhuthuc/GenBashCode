"""Request classifier: Vietnamese request (never the command) -> Prediction(ambiguous, expected_effects).

One encoder (XLM-R: copes with accent-less / mixed-English requests without a word
segmenter) + one linear head of 1 + len(EFFECTS) logits: [ambiguous, *effects].
Trained on scripts/autolabel.py output; effect targets are masked where the gold
command gave no label (vague templates, opaque commands).

  python -m vishell.classifier train --data data/classifier --out models/classifier
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Callable

from ..analyzer import EFFECTS
from ..policy import Prediction

MAX_LEN = 64


def encode_labels(row: dict) -> tuple[list[float], list[float]]:
    """-> (targets, mask) over [ambiguous, *EFFECTS]; mask 0 = no label for that slot."""
    eff = row.get("effects")
    targets = [float(row["ambiguous"])] + [float(eff is not None and e in eff) for e in EFFECTS]
    mask = [1.0] + [float(eff is not None)] * len(EFFECTS)
    return targets, mask


def masked_bce(logits, targets, mask, pos_weight=None):
    import torch.nn.functional as F
    loss = F.binary_cross_entropy_with_logits(logits, targets, reduction="none", pos_weight=pos_weight) * mask
    return loss.sum() / mask.sum().clamp(min=1)


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _model(name: str):
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(name)
    enc = AutoModel.from_pretrained(name)
    head = torch.nn.Linear(enc.config.hidden_size, 1 + len(EFFECTS))
    return tok, enc, head


def _logits(tok, enc, head, texts: list[str]):
    batch = tok(texts, padding=True, truncation=True, max_length=MAX_LEN, return_tensors="pt").to(enc.device)
    h = enc(**batch).last_hidden_state
    m = batch["attention_mask"].unsqueeze(-1).to(h.dtype)
    return head((h * m).sum(1) / m.sum(1))  # mean pooling


def train(data_dir: str, out_dir: str, model_name: str = "FacebookAI/xlm-roberta-base",
          epochs: int = 8, lr: float = 3e-5, batch_size: int = 16, seed: int = 42) -> dict:
    import torch

    random.seed(seed)
    torch.manual_seed(seed)
    rows = _read(Path(data_dir) / "train.jsonl")
    val = _read(Path(data_dir) / "val.jsonl")
    tok, enc, head = _model(model_name)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    enc.to(device), head.to(device)
    opt = torch.optim.AdamW(list(enc.parameters()) + list(head.parameters()), lr=lr, weight_decay=0.01)
    # Rare effects (delete 4%, perm_change 3% of rows vs write 30%) otherwise collapse to "write".
    t_all, m_all = (torch.tensor(x) for x in zip(*(encode_labels(r) for r in rows)))
    pos = (t_all * m_all).sum(0)
    pos_weight = ((m_all.sum(0) - pos) / pos.clamp(min=1)).clamp(1, 10).to(device)
    steps = epochs * ((len(rows) + batch_size - 1) // batch_size)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / (0.1 * steps)) * max(0.0, 1 - s / steps))

    log = []
    for ep in range(epochs):
        enc.train(), head.train()
        random.shuffle(rows)
        total = 0.0
        for i in range(0, len(rows), batch_size):
            b = rows[i:i + batch_size]
            t, m = zip(*(encode_labels(r) for r in b))
            loss = masked_bce(_logits(tok, enc, head, [r["text"] for r in b]),
                              torch.tensor(t, device=device), torch.tensor(m, device=device), pos_weight)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(list(enc.parameters()) + list(head.parameters()), 1.0)
            opt.step()
            sched.step()
            total += loss.item() * len(b)
        metrics = evaluate(lambda texts: _probs(tok, enc, head, texts), val)
        log.append({"epoch": ep + 1, "train_loss": total / len(rows), **metrics})
        print(json.dumps(log[-1]))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    enc.save_pretrained(out)
    tok.save_pretrained(out)
    torch.save(head.state_dict(), out / "head.pt")
    (out / "classifier.json").write_text(json.dumps({"effects": EFFECTS, "threshold": 0.5, "log": log}, indent=1))
    return log[-1]


def _probs(tok, enc, head, texts: list[str], batch_size: int = 64) -> list[list[float]]:
    import torch
    enc.eval(), head.eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), batch_size):
            out += torch.sigmoid(_logits(tok, enc, head, texts[i:i + batch_size])).tolist()
    return out


def evaluate(probs_fn: Callable[[list[str]], list[list[float]]], rows: list[dict], threshold: float = 0.5) -> dict:
    """Ambiguity F1 and effect micro-F1 (labelled rows only), plus exact effect-set match."""
    probs = probs_fn([r["text"] for r in rows])
    tp = fp = fn = etp = efp = efn = exact = n_eff = 0
    for r, p in zip(rows, probs):
        amb = p[0] >= threshold
        tp += amb and r["ambiguous"]
        fp += amb and not r["ambiguous"]
        fn += (not amb) and r["ambiguous"]
        if r.get("effects") is not None:
            pred = {e for e, q in zip(EFFECTS, p[1:]) if q >= threshold}
            gold = set(r["effects"])
            etp += len(pred & gold)
            efp += len(pred - gold)
            efn += len(gold - pred)
            exact += pred - {"read"} == gold - {"read"}
            n_eff += 1

    def f1(a, b, c):
        return 2 * a / (2 * a + b + c) if a else 0.0
    return {"ambiguity_f1": round(f1(tp, fp, fn), 4), "effects_micro_f1": round(f1(etp, efp, efn), 4),
            "effects_exact": round(exact / n_eff, 4) if n_eff else None}


def load(model_dir: str) -> Callable[[str], Prediction]:
    """-> classify(request) for policy.decide()."""
    import torch
    from transformers import AutoModel, AutoTokenizer

    d = Path(model_dir)
    cfg = json.loads((d / "classifier.json").read_text())
    assert cfg["effects"] == EFFECTS, "effect taxonomy changed since this classifier was trained"
    tok, enc = AutoTokenizer.from_pretrained(d), AutoModel.from_pretrained(d)
    head = torch.nn.Linear(enc.config.hidden_size, 1 + len(EFFECTS))
    head.load_state_dict(torch.load(d / "head.pt", map_location="cpu"))
    thr = cfg["threshold"]

    def classify(request: str) -> Prediction:
        p = _probs(tok, enc, head, [request])[0]
        return Prediction(p[0] >= thr, {e for e, q in zip(EFFECTS, p[1:]) if q >= thr})

    classify.probs = lambda texts: _probs(tok, enc, head, texts)
    return classify


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m vishell.classifier")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--data", default="data/classifier")
    t.add_argument("--out", default="models/classifier")
    t.add_argument("--model", default="FacebookAI/xlm-roberta-base")
    t.add_argument("--epochs", type=int, default=8)
    e = sub.add_parser("eval")
    e.add_argument("--model-dir", default="models/classifier")
    e.add_argument("--data", default="data/classifier/test.jsonl")
    a = ap.parse_args()
    if a.cmd == "train":
        train(a.data, a.out, a.model, a.epochs)
    else:
        print(json.dumps(evaluate(load(a.model_dir).probs, _read(Path(a.data)))))
