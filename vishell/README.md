# ViShell

Model nhỏ (Qwen2.5-Coder-1.5B-Instruct) nhận yêu cầu tiếng Việt, quyết định
`execute` / `probe` / `ask` và sinh lệnh bash tương ứng, huấn luyện bằng GRPO với
reward tính hoàn toàn từ thực thi sandbox + hash filesystem + phân loại khả năng
đảo ngược (bashlex) — không dùng LLM làm giám khảo. Xem `AGENT.md` (thư mục cha) cho
đặc tả đầy đủ, `PLAN.md` cho lộ trình, `DECISIONS.md` cho các quyết định thiết kế.

## Cài đặt

```bash
pip install -e .[dev]           # core + pytest, đủ để chạy mọi thứ không cần GPU
pip install -e .[data]          # + requests/pandas cho bước dịch dữ liệu
pip install -e .[train]         # + unsloth/trl/peft/torch — chỉ cần trên Colab/Kaggle (GPU)
pip install -e .[demo]          # + gradio
```

Sandbox thực thi cần **Docker Desktop** (Windows) hoặc `unshare` (Linux/Colab/Kaggle).

## Chạy

```bash
python -m vishell verify --config configs/smoke.yaml       # kiểm chứng template qua sandbox
python -m vishell pipeline --config configs/smoke.yaml     # toàn bộ pipeline, bỏ qua bước GPU
python -m vishell pipeline --config configs/colab_t4.yaml  # trên Colab/Kaggle: chạy thật kể cả GPU
```

Mỗi bước cũng gọi riêng được: `data-nl2bash`, `verify`, `build-dataset`, `sft-nl2bash`,
`merge-nl2bash`, `sft-scenarios`, `merge-scenarios`, `grpo`, `merge-grpo`, `export`,
`evaluate`, `report`, `demo`. Override bất kỳ giá trị config nào bằng `--set a.b=c`.

## Safety module (LLM chỉ sinh lệnh, module riêng quyết định)

```bash
python scripts/autolabel.py                                   # nhãn effects/ambiguous từ AST lệnh gold -> data/classifier/
python -m vishell.classifier train                            # XLM-R: request -> ambiguous + expected effects
python -m vishell.generator --model <dir> --items <jsonl> --k 3 --out <jsonl>   # GPU: k mẫu bash/NONE
python -m vishell.eval [--templates predictions_templates.jsonl]               # so sánh (a) LLM, (b) luật, (c) hybrid
```

`analyzer/` (bashlex + `rules.yaml`) -> effects/scope/risk; `policy/` -> run/confirm/regenerate/
ask_clarification/block. Risk cuối luôn là risk của AST; phân loại request chỉ có thể làm chặt hơn.
`envcheck/` (khi gọi `decide(..., root=<workspace>)`): nhìn file thật mà lệnh đụng tới — có secret
(detect-secrets) thì block; owner khác/mode 600/không ghi được thì ít nhất dangerous; ngoài git +1; có snapshot −1;
dry run trong sandbox (`backend=`) đổi quá `max_changed` file thì confirm. Cần `pip install -e .[env]`.

```bash
python scripts/envsignals.py rules --dry-run    # (b) luật path vs (c) path + tín hiệu môi trường, Linux
python scripts/envsignals.py run <tên> <model>  # (a) model tự quyết (GPU), rồi: table <tên>
```

## Cấu trúc

```
vishell/            # package chính (xem PLAN.md cho thứ tự đọc code)
configs/            # default.yaml + override cho smoke/colab_t4/colab_a100
templates/          # 158 kịch bản (json), 158/158 đã qua kiểm chứng sandbox
docker/             # image sandbox
notebooks/          # notebook Colab mỏng: mount, cài đặt, gọi `python -m vishell pipeline`
scripts/             # start_vllm.sh (server dịch/eval OpenAI-compatible)
tests/               # pytest cho mọi module
```

## Kiểm thử

```bash
pytest -q                                    # phần không cần GPU/Docker (bị skip nếu thiếu)
python -m vishell verify --config configs/smoke.yaml    # cần Docker
```
