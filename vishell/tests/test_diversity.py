import numpy as np

from vishell.diversity import score_templates

CFG = {"sem_percentile": 95, "max_lexical": 0.5, "min_good_pair_frac": 0.6, "min_requests": 2}


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_paraphrase_passes_duplicate_and_off_meaning_fail():
    docs = [
        ["sao_lưu", "dữ_liệu"], ["backup", "project", "này"],         # 0: same meaning, different words
        ["xoá", "file", "log"], ["xoá", "file", "log", "đi"],          # 1: near-duplicate wording
        ["đếm", "số", "dòng"], ["khởi_động", "lại", "máy"],             # 2: different words AND meaning
    ]
    emb = _unit([[1, 0.05, 0], [1, 0, 0.05],
                 [0, 1, 0.05], [0.05, 1, 0],
                 [0, 0, 1], [0.7, 0.7, 0]])
    res = score_templates([[0, 1], [2, 3], [4, 5]], docs, emb, CFG)
    good = [t["pass"] for t in res["templates"]]
    assert good == [True, False, False], res
    assert res["templates"][1]["near_duplicates"] and not res["templates"][1]["off_meaning"]
    assert res["templates"][2]["off_meaning"]


def test_too_few_requests_fails_even_if_diverse():
    docs = [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]]
    emb = _unit([[1, 0.05], [1, 0], [0, 1], [0.05, 1]])
    res = score_templates([[0, 1], [2, 3]], docs, emb, {**CFG, "min_requests": 3})
    assert not any(t["pass"] for t in res["templates"])
