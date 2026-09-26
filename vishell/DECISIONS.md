# DECISIONS — các lựa chọn không hiển nhiên và lý do

## Kết quả kiểm chứng template (kết luận)
158 template (76 execute, 42 probe, 20 ask/ambiguous, 20 ask/irreversible) — **158/158
pass** qua sandbox Docker thật ở lượt kiểm chứng cuối. Toàn bộ lỗi phát hiện được (hơn 20
lỗi thật, không phải lỗi authoring hời hợt) đã ghi lại nguyên nhân + cách sửa trong các
mục bên dưới, phần lớn rơi vào 3 nhóm: (1) bug trong chính `classify.py` mà bộ template
này tình cờ phơi bày (git branch, sed pattern, `/dev/null`, redirect trên compound node,
bashlex không hỗ trợ `$(( ))`), (2) check quá lỏng (wrong_command vô tình đúng do hai
tham số ngẫu nhiên trùng nhau, hoặc do dùng `stdout_lines_set` cho lệnh mà thứ tự mới là
điều cần kiểm), (3) công cụ không có sẵn trong sandbox image (`rsync`, `xz` — bỏ template
thay vì mở rộng image, để giữ đúng danh sách công cụ AGENT.md mục 4 đã quy định).

## Dữ liệu có sẵn (`finals/`, `models/sft_v1/`, `data/nl2bash_vi/`)
- `finals/` (adapter LoRA, root repo) là kết quả một lần chạy **SMOKE** trên Kaggle
  (20 bước, 48 mẫu, target ở dạng **plain bash**, không phải JSON action). Pipeline này
  **không** dùng `finals/` làm `models/sft_v1/`: định dạng target khác (plain bash vs JSON
  `{action,command,question}`) và cỡ mẫu chỉ mang tính smoke-test, không đại diện.
- `models/sft_v1/` và `data/nl2bash_vi/` (nêu trong AGENT.md §2) chưa có trên máy tại thời
  điểm viết; người dùng sẽ tự chuyển vào sau. `cli.py` (`step_sft`/`step_data_nl2bash`)
  kiểm tra sự tồn tại của các đường dẫn này trước khi chạy — có thì dùng lại (skip), không
  thì train mới / báo lỗi rõ ràng yêu cầu trỏ tới dữ liệu thật thay vì đoán.
- Cột dữ liệu dịch (`nl_vi`/`vi`/`description_vi`, `cmd`/`bash`/`command`) được nhận dạng
  tự động (`data/nl2bash.py:load_existing_vi`), không hard-code tên cột.

## classify.py (bashlex)
- **Mạng luôn là R2/blocked**, kể cả HTTP GET: AGENT.md liệt kê "HTTP GET" là ví dụ probe
  ở góc nhìn *hành động của model* (mục 1), nhưng ở phần đo lường đảo ngược (mục 1, định
  nghĩa R2) lại liệt "mạng: gửi/nhận dữ liệu (curl, wget...)" là R2 vô điều kiện, và sandbox
  luôn chặn mạng hoàn toàn (`--network none`). classify.py đi theo định nghĩa R2 cơ học này
  vì đó là thứ nó thực sự đo được; phân biệt GET/POST là việc của người viết template khi
  gán `expected_action`, không phải của classify.py.
- **`/dev/null`, `/dev/stdout`, `/dev/stderr`** được coi là "safe sink", không tính là ra
  ngoài workspace — `2>/dev/null` để nuốt lỗi là thành ngữ phổ biến, vô hại, không nên đẩy
  một lệnh chỉ-đọc lên R1/R2.
- **`sed`/`grep`/`awk`**: chỉ kiểm tra "path ra ngoài workspace" trên các đối số VỊ TRÍ SAU
  đối số đầu tiên (pattern/script), vì pattern kiểu `/ERROR/d` hay `/^foo/` trông giống path
  tuyệt đối nhưng không phải.
- **`basename`/`dirname`**: không kiểm tra path ra ngoài workspace — đây là phép xử lý
  chuỗi thuần tuý, không có I/O trên hệ thống file, dù tham số trông giống path tuyệt đối.
- **`git branch <tên>`** là mutation (R1, tạo nhánh mới); **`git branch`** (không tham số,
  hoặc chỉ cờ liệt kê) là read-only (R0). Cùng tên lệnh nhưng hai hành vi rất khác nhau.
- **`sort file -o out`** là R1 (ghi ra file), khác với `sort file` (chỉ in ra stdout, R0).
- **`pip`/`npm`/`cargo`/`go install`** được xếp R1 (execute) theo đúng ví dụ "package
  install" trong AGENT.md mục 1 (execute), giả định cài đặt cục bộ/venv trong workspace;
  **`apt`/`yum`/`dnf`/`pacman`** luôn R2 vì luôn là cài đặt toàn hệ thống, không có biến thể
  cục bộ. Vì sandbox không có mạng, một lệnh pip/npm thật sự cần tải gói sẽ tự thất bại và
  bị runner.py gắn `boundary_violation` (đo động) — lớp đo tĩnh (classify) không cần giải
  quyết vấn đề này một cách hoàn hảo, lớp đo động sẽ bắt được.

## Sandbox image (`docker/Dockerfile.sandbox`)
Chỉ có: python3, coreutils, findutils, grep, sed, gawk, jq, bc, tree, zip/unzip, file,
procps, git — đúng danh sách AGENT.md mục 4. Template KHÔNG dùng make/gcc/node/npm thật sự
(không có trong image); các ví dụ "build/test" dùng `python3` (luôn có sẵn vì base image
chính là `python:3.12-slim`).

`find -executable` (GNU findutils) trả về sai (không nhận diện file đã `chmod +x`) trong
container này — có vẻ liên quan tới cách `access()`/seccomp mặc định của Docker Desktop xử
lý bit thực thi. Template dùng `find -perm -u+x` thay thế, đã kiểm chứng đúng.

## Check design cho template (rút ra khi verify)
- Probe **không bao giờ** dùng `check_type: script` — script chỉ thấy trạng thái
  filesystem sau khi lệnh chạy xong, không thấy stdout của lệnh; với probe (không đổi fs),
  một script check sẽ đúng cho MỌI lệnh kể cả no-op, khiến kiểm chứng (c) luôn fail.
- `stdout_lines_set` (so khớp không quan tâm thứ tự) không phù hợp để kiểm tra một lệnh
  MÀ Ý NGHĨA CHÍNH LÀ THỨ TỰ (vd `sort`) — một lệnh "sai" nhưng cho cùng tập dòng (vd `cat`
  thay vì `sort`) sẽ vẫn "pass" sai. Các template loại sắp xếp dùng `stdout_equals` với nội
  dung được dựng sao cho thứ tự đúng luôn suy ra được từ tham số (chung tiền tố, khác hậu tố
  a/b/c) — tránh phải làm phép tính hay rẽ nhánh trong `check_expected` (nó chỉ là chuỗi
  thay tham số thuần tuý, không phải template engine).
- `check_expected` của `check_type: script` được CHẠY như bash thật ở thời điểm kiểm tra,
  nên phép tính `$(( {n} + 1 ))` hợp lệ ở đó — khác với `stdout_equals`/`stdout_contains`/
  `stdout_lines_set`, nơi `check_expected` chỉ là một chuỗi so khớp, không được tính toán.

## bashlex limitations that shaped classify.py
- **Không hỗ trợ arithmetic expansion `$(( ))`**: bashlex ném lỗi parse trên bất kỳ
  `$((...))` nào (đã xác nhận trực tiếp). Template dùng `expr $i + 1` qua command
  substitution `$(...)` thay cho `$((i+1))` để tránh bị coi là "không parse được → R2".
  Hệ quả thật: một lệnh do model sinh ra có dùng `$(( ))` cũng sẽ bị classify.py đánh giá
  quá thận trọng (R2 → ask) dù thực chất vô hại — chấp nhận được vì đây là hướng an toàn
  ("không chắc thì ask"), không phải hướng nguy hiểm.
- **Redirect trên compound node** (`{ cmd1; cmd2; } > out.txt`, subshell `( ... ) > out`)
  nằm ở thuộc tính `.redirects` của chính compound node, KHÁC với redirect của một lệnh
  đơn (nằm trong `.parts` của `CommandNode`). `_walk()` phải kiểm tra cả hai chỗ, nếu
  không sẽ bỏ sót redirect và chấm sai mức (vd coi nhầm việc ghi ra file mới là R0).

## Bẫy khi viết template (đã tự gặp phải)
- Không được viết `${ten_tham_so}` khi Ý ĐỊNH là thay tham số — `fill_params` cố tình
  KHÔNG thay `${x}` (để không phá cú pháp biến shell thật), nên nếu tự ý dùng `${name}`
  cho tham số `name` của mình, nó sẽ giữ nguyên y hệt `${name}` trong lệnh cuối, không
  hề được điền giá trị. Dùng lệnh khác không cần `$` ngay trước dấu `{` (vd `printenv
  {name}` thay vì `echo ${name}`) khi cần tham số nằm sát sau ký tự `$`.
- Đừng viết `{{`/`}}` kiểu Python `.format()` — `fill_params` chỉ thay đúng `{ten}`, dấu
  ngoặc nhọn đôi sẽ giữ nguyên y hệt (thành cú pháp Python/bash sai, vd dict literal
  `{{'k': 'v'}}` thay vì `{'k': 'v'}`).
- Với `check_type` khác `script`, `check_expected` chỉ được so khớp CHUỖI — không được
  tính toán hay rẽ nhánh theo tham số nào được chọn. Nếu hai tham số cần cộng lại
  (vd tổng số dòng của 2 file), gộp thành MỘT tham số rồi chia trong `setup` bằng số học
  bash thật (`$(( total / 2 ))` — hợp lệ vì `setup` không đi qua classify.py); nếu thứ tự
  đầu ra phụ thuộc giá trị tham số (vd sắp xếp), chọn tham số sao cho thứ tự đúng LUÔN
  suy ra được (chung tiền tố, khác hậu tố cố định a/b/c) thay vì tính toán.
- `wrong_commands` phải THẬT SỰ sai trong MỌI trường hợp tham số có thể rơi vào, không chỉ
  "thường sai" — vd `sed -i '1d'` chỉ sai nếu tham số dòng cần xoá không phải là 1; nếu
  tham số có thể bằng 1, đây là wrong_command tồi. Chọn phương án luôn khác biệt cấu trúc
  (`sed -i '$d'` xoá dòng cuối, không bao giờ trùng dòng đầu trong phạm vi tham số của
  template) thay vì một giá trị cụ thể có thể trùng ngẫu nhiên.
- **Bài học thật từ việc đổi seed**: lô kiểm chứng đầu (seed=0, thủ công) cho 158/158 pass,
  nhưng chạy lại qua CLI thật (seed=42, mặc định của `default.yaml`) lộ ra 8 template lỗi
  — cùng một lỗi CÓ THỂ tồn tại âm thầm nếu bộ tham số ngẫu nhiên của một seed cụ thể tình
  cờ không rơi vào tổ hợp gây lỗi. Nguyên nhân phổ biến nhất: hai tham số `int` được sinh
  ĐỘC LẬP (vd `n_match`, `n_other`) mà `wrong_command` đo tham số còn lại — hai tham số này
  ngẫu nhiên bằng nhau (~15-25% mỗi lần sinh) khiến "lệnh sai" vô tình đúng. Cách sửa chung:
  không sinh hai tham số độc lập khi test cần chúng LUÔN khác nhau — chỉ khai báo MỘT tham
  số, suy tham số còn lại bằng số học bash ngay trong `setup` (`n_other=$(( {n} + 1 ))`;
  hợp lệ vì `setup` không đi qua `classify.py`). Đã quét toàn bộ `templates/*.json` tìm mọi
  cặp tham số `int` độc lập và sửa theo mẫu này ở nơi có rủi ro thật.
- `check_type: stdout_contains` so khớp CHUỖI CON, không phải cả dòng — `"{prefix}{n}"` có
  thể vô tình là tiền tố của một số dài hơn xuất hiện trong output (vd tìm "line2" nhưng
  output có "line23", "line24"...). Thêm `\n` vào cuối `check_expected` (mỗi dòng `echo` ra
  luôn có `\n` theo sau) để chỉ khớp đúng dòng, không khớp nhầm một số khác có cùng tiền tố.

## Reward (`rewards.py`)
Hai ô trong bảng AGENT.md §6.5 không ghi rõ giá trị khi điều kiện phụ không đạt (probe
đúng hàng nhưng fs vẫn đổi ở hai hàng "ask"): áp dụng cùng mức phạt -1.0 như các ô
"probe làm đổi fs" khác trong bảng, để nhất quán thay vì bịa một hằng số mới.
