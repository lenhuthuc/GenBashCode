# Paper draft — trạng thái

`main.tex` là bản nháp (class `article`, biên dịch: `pdflatex main && bibtex main && pdflatex main && pdflatex main`).
Mọi con số lấy từ repo; chỗ đỏ `[TODO: ...]` là kết quả **chưa có** — không nộp khi còn TODO.

## Còn thiếu (theo thứ tự ưu tiên)

| # | Việc | Ai | Cách làm | Điền vào |
|---|---|---|---|---|
| 1 | Tên + phiên bản model lớn trong `eval_sets/risk_2x2/result.md` | bạn | nhớ lại đã dán vào chat nào | Bảng 1, abstract |
| 2 | Phép thử 2×2 cho Coder 0.5B / 3B / 7B + `judge` cho mọi model | bạn (Colab) | notebook, phần "Phép thử 2×2 theo cỡ model" | Bảng 1, §5 Recognition, Scale |
| 3 | Bộ mở rộng do người khác viết (≥ 30 nhóm, ≥ 2 người) | bạn nhờ người | `eval_sets/risk_2x2_ext/README.md` | §5 Independent test set |
| 4 | Người gán nhãn thứ hai + kappa | bạn nhờ người | `risk2x2.py annotate-export` → `kappa` | §5 |
| 5 | Chạy lại model lớn từng câu qua API (cùng giao thức) | bạn (cần API key) | `risk2x2.py run` với endpoint, hoặc chat từng câu | Bảng 1 |
| 6 | Ngưỡng mơ hồ chọn trên val | mình | thêm vào `classifier` + `eval` | §6 |
| 7 | Thử nhanh "kiểm tra đối tượng" | mình | script thử trên output đã lưu | §6 |
| 8 | Tìm tài nguyên NL→code/Bash tiếng Việt đã có | bạn + mình | Google Scholar, ACL Anthology | §2 |
| 9 | Kiểm tra lại từng mục trong `refs.bib` (viết từ trí nhớ) | bạn | trang của nhà xuất bản / ACL Anthology | cuối bài |
| 10 | Đổi sang template ACL, tác giả, cảm ơn người viết/gán nhãn | bạn | | |

## Venue gợi ý
Workshop tại ACL/EMNLP (NLP cho ngôn ngữ ít tài nguyên, đánh giá/an toàn LLM), hoặc hội nghị khu vực
(KSE, RIVF, SoICT, PACLIC). Đăng arXiv sau khi xong mục 1–4.
