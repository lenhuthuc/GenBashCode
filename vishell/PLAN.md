# PLAN — ViShell

Xây pipeline RL end-to-end cho model nhỏ (Qwen2.5-Coder-1.5B-Instruct) sinh JSON
`{"action": execute|probe|ask, "command", "question"}` từ yêu cầu tiếng Việt, với reward
tính hoàn toàn từ sandbox thực thi + hash filesystem + phân loại khả năng đảo ngược
(bashlex), không dùng LLM làm giám khảo. Chi tiết đầy đủ ở `AGENT.md` (thư mục cha).

## Thứ tự thực hiện

1. **Nền tảng** (không cần GPU, chạy trên laptop): `paths.py`, `schema.py`, `prompts.py`,
   `classify.py`, `noise.py` — mỗi module có pytest riêng.
2. **Sandbox**: `sandbox/runner.py` (chuẩn stdlib, chạy được cả trong container lẫn
   `unshare`) + `sandbox/backends.py` (Docker cho Windows, Local/unshare cho Linux) +
   `docker/Dockerfile.sandbox`. Kiểm bằng cách thực thi thật (mutate/probe/timeout/
   network-block/read-only-block) qua Docker trước khi tin bất kỳ chỉ số nào dựa trên nó.
3. **Reward**: `rewards.py` implement đúng bảng hệ số ở AGENT.md §6.5, test từng ô.
4. **Template + verify**: viết `templates/*.json` theo lô 20, chạy `python -m vishell verify`
   sau mỗi lô, sửa tối đa 3 lần/template rồi loại nếu vẫn fail. Mục tiêu ≥150 template.
5. **Dữ liệu**: `data/nl2bash.py` (NL2Bash GitHub → dịch, lọc bashlex) và
   `data/templates.py` (điền tham số, sinh instance, split theo template_id).
6. **Training** (chỉ chạy được có GPU — Colab/Kaggle): `train/sft.py` (2 lượt: nl2bash rồi
   scenarios) → `train/merge.py` → `train/grpo.py` → `train/merge.py` → `train/export.py`.
7. **Đánh giá + báo cáo**: `evaluate.py` (mọi hệ, sạch/nhiễu) → `report.py` → REPORT.md.
8. **Demo**: `demo.py` (Gradio, thao tác trên thư mục thật của người dùng).

## Trạng thái triển khai

Toàn bộ mã nguồn ở mục trên đã viết xong và có pytest đi kèm (`tests/`, 91 test không cần
Docker + 17 test cần Docker, tất cả pass). Sandbox đã kiểm chứng thật qua Docker (không
phải mock). **158/158 template đã qua kiểm chứng động** (`results/verify.json` sau khi
chạy `verify`, vượt mục tiêu ≥150 của AGENT.md): 76 execute, 42 probe, 40 ask (20 ambiguous
+ 20 irreversible) — gần đúng tỉ lệ 60/20/20 đề ra ở mục 5. Toàn bộ pipeline không-cần-GPU (`data-nl2bash` → `build-dataset` →
`evaluate` với oracle/mock → `report`) đã chạy thật qua CLI, không chỉ unit test, và
`REPORT.md` sinh ra đúng như thiết kế. Xem DECISIONS.md cho các đánh đổi cụ thể (công cụ
nào có sẵn trong sandbox image, giới hạn bashlex với `$(( ))`, các bẫy khi viết template).
Các bước cần GPU (train/*, evaluate với hệ base/sft/grpo) chưa chạy thật trên máy này —
chạy qua `notebooks/colab_pipeline.ipynb` trên Colab/Kaggle.

## Không làm (YAGNI)

- Không thêm backend sandbox thứ ba (vd Firecracker/gVisor) — Docker + unshare đã đủ theo
  yêu cầu AGENT.md §4.
- Không tự viết lại vLLM client đầy đủ — chỉ gọi endpoint OpenAI-compatible bằng
  `urllib` (đủ dùng, không cần thêm dependency `openai`).
- Không build UI riêng cho việc duyệt template — `results/verify.json` + log console là đủ
  cho quy mô ~150 template của một người viết.
