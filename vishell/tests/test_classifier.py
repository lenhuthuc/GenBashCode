import pytest

from vishell.analyzer import EFFECTS
from vishell.classifier import encode_labels, evaluate

torch = pytest.importorskip("torch")


def test_masked_effects_get_no_gradient():
    from vishell.classifier import masked_bce
    t1, m1 = encode_labels({"ambiguous": True, "effects": None})
    t2, m2 = encode_labels({"ambiguous": False, "effects": ["delete"]})
    assert m1 == [1.0] + [0.0] * len(EFFECTS) and t2[1 + EFFECTS.index("delete")] == 1.0
    logits = torch.zeros(2, 1 + len(EFFECTS), requires_grad=True)
    masked_bce(logits, torch.tensor([t1, t2]), torch.tensor([m1, m2])).backward()
    assert torch.all(logits.grad[0, 1:] == 0) and logits.grad[0, 0] != 0 and torch.any(logits.grad[1, 1:] != 0)


def test_evaluate_scores_perfect_predictions():
    rows = [{"text": "a", "ambiguous": True, "effects": None},
            {"text": "b", "ambiguous": False, "effects": ["read", "delete"]}]
    gold = {"a": [1.0] + [0.0] * len(EFFECTS), "b": [0.0] + [float(e in ("read", "delete")) for e in EFFECTS]}
    m = evaluate(lambda texts: [gold[t] for t in texts], rows)
    assert m == {"ambiguity_f1": 1.0, "effects_micro_f1": 1.0, "effects_exact": 1.0}
