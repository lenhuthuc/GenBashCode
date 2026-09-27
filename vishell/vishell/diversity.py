"""Paraphrase diversity of each template's `requests_vi`: two requests of one template should
mean the same (PhoBERT-SimCSE cosine high) but be worded differently (TF-IDF cosine and BM25
low). Text is word-segmented with VnCoreNLP first, as PhoBERT expects. Needs Java (VnCoreNLP);
CPU is enough, so run it on Colab without a GPU.

Lexical similarity ignores parameter values: every paraphrase must name the same file/path, and
that shared argument is not wording. Semantic similarity sees the filled-in request.

The semantic threshold is calibrated, not guessed: a within-template pair only counts as "same
meaning" if it is closer than `sem_percentile`% of cross-template pairs (different intents)."""
from __future__ import annotations

import math
import os
import random
from collections import Counter
from itertools import combinations
from pathlib import Path

import numpy as np

from .data.templates import fill_params, sample_params


def template_texts(template) -> tuple[list[str], list[str]]:
    """(filled, stripped): requests_vi with one fixed parameter draw, so `{param}` reads as real
    words for the embedding model, and with parameters removed, for the lexical measures."""
    params = sample_params(template, random.Random(f"diversity:{template.template_id}"))
    blank = {k: " " for k in params}
    return ([fill_params(r, params) for r in template.requests_vi],
            [fill_params(r, blank) for r in template.requests_vi])


def segment(texts: list[str], save_dir: str | Path) -> list[str]:
    import py_vncorenlp

    save_dir = Path(save_dir).resolve()
    save_dir.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    try:
        py_vncorenlp.download_model(save_dir=str(save_dir))  # no-op when already there
        seg = py_vncorenlp.VnCoreNLP(annotators=["wseg"], save_dir=str(save_dir))
    finally:
        os.chdir(cwd)  # VnCoreNLP chdirs into save_dir
    return [" ".join(seg.word_segment(t)) for t in texts]


def embed(segmented: list[str], model_name: str, batch: int = 64) -> np.ndarray:
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name).eval()
    out = []
    with torch.no_grad():
        for i in range(0, len(segmented), batch):
            enc = tok(segmented[i:i + batch], padding=True, truncation=True, max_length=128, return_tensors="pt")
            out.append(model(**enc).pooler_output)  # SimCSE checkpoints are trained on pooler_output
    e = torch.cat(out).float().numpy()
    return e / np.linalg.norm(e, axis=1, keepdims=True)


def tfidf_cosine(docs: list[list[str]]) -> np.ndarray:
    from sklearn.feature_extraction.text import TfidfVectorizer

    m = TfidfVectorizer(analyzer=lambda toks: toks).fit_transform(docs)  # rows are L2-normalised
    return (m @ m.T).toarray()


def bm25_pair(docs: list[list[str]], i: int, j: int, idf: dict, avgdl: float, k1=1.5, b=0.75) -> float:
    """BM25 of i against j normalised by i against itself, averaged both ways -> [0, 1]."""
    def score(q: list[str], d: list[str]) -> float:
        tf = Counter(d)
        return sum(idf[t] * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl)) for t in set(q) if t in tf)

    def norm(a, c):
        self_score = score(docs[a], docs[a])
        return score(docs[a], docs[c]) / self_score if self_score else 0.0

    return (norm(i, j) + norm(j, i)) / 2


def score_templates(groups: list[list[int]], docs: list[list[str]], emb: np.ndarray, cfg: dict) -> dict:
    """groups[g] = indices (into docs/emb) of template g's requests. Pure, so it is testable."""
    sim = emb @ emb.T
    owner = np.empty(len(docs), dtype=int)
    for g, idx in enumerate(groups):
        owner[idx] = g
    iu = np.triu_indices(len(docs), k=1)
    cross = sim[iu][owner[iu[0]] != owner[iu[1]]]
    sem_thr = float(np.percentile(cross, cfg["sem_percentile"])) if cross.size else 0.0

    n = len(docs)
    df = Counter(t for d in docs for t in set(d))
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    avgdl = sum(map(len, docs)) / n
    tfidf = tfidf_cosine(docs)

    per_template = []
    for g, idx in enumerate(groups):
        pairs = []
        for i, j in combinations(idx, 2):
            s, t, b = float(sim[i, j]), float(tfidf[i, j]), bm25_pair(docs, i, j, idf, avgdl)
            pairs.append({"i": i, "j": j, "semantic": s, "tfidf": t, "bm25": b,
                          "good": s >= sem_thr and t <= cfg["max_lexical"] and b <= cfg["max_lexical"]})
        good_frac = sum(p["good"] for p in pairs) / len(pairs) if pairs else 0.0
        per_template.append({
            "n_requests": len(idx), "good_pair_frac": good_frac,
            "mean_semantic": float(np.mean([p["semantic"] for p in pairs])) if pairs else 0.0,
            "mean_tfidf": float(np.mean([p["tfidf"] for p in pairs])) if pairs else 0.0,
            "mean_bm25": float(np.mean([p["bm25"] for p in pairs])) if pairs else 0.0,
            "near_duplicates": [(p["i"], p["j"]) for p in pairs if p["tfidf"] > cfg["max_lexical"]],
            "off_meaning": [(p["i"], p["j"]) for p in pairs if p["semantic"] < sem_thr],
            "pass": len(idx) >= cfg["min_requests"] and good_frac >= cfg["min_good_pair_frac"],
        })
    return {"semantic_threshold": sem_thr, "templates": per_template}


def run_diversity(templates: list, cfg: dict, vncorenlp_dir: str | Path) -> dict:
    texts, stripped, groups = [], [], []
    for t in templates:
        req, bare = template_texts(t)
        groups.append(list(range(len(texts), len(texts) + len(req))))
        texts += req
        stripped += bare
    segmented = segment(texts + stripped, vncorenlp_dir)
    seg_filled, seg_bare = segmented[:len(texts)], segmented[len(texts):]
    docs = [s.lower().split() for s in seg_bare]
    result = score_templates(groups, docs, embed(seg_filled, cfg["embed_model"]), cfg)
    for t, idx, row in zip(templates, groups, result["templates"]):
        row["template_id"] = t.template_id
        row["near_duplicates"] = [(texts[i], texts[j]) for i, j in row["near_duplicates"]]
        row["off_meaning"] = [(texts[i], texts[j]) for i, j in row["off_meaning"]]
    rows = result["templates"]
    result["summary"] = {
        "n_templates": len(rows), "n_pass": sum(r["pass"] for r in rows),
        "mean_good_pair_frac": float(np.mean([r["good_pair_frac"] for r in rows])),
        "mean_requests": float(np.mean([r["n_requests"] for r in rows])),
        "thresholds": {k: cfg[k] for k in ("sem_percentile", "max_lexical", "min_good_pair_frac", "min_requests")},
    }
    return result
