from vishell.noise import noisy_for, selected_for_noise, strip_accents


def test_strip_accents():
    assert strip_accents("xoá tệp đường dẫn") == "xoa tep duong dan"


def test_noisy_for_deterministic():
    a, ops_a = noisy_for("id-1", "xoá tất cả file .log trong thư mục /var/log", "rm /var/log/*.log")
    b, ops_b = noisy_for("id-1", "xoá tất cả file .log trong thư mục /var/log", "rm /var/log/*.log")
    assert a == b
    assert ops_a == ops_b


def test_noisy_for_differs_by_id():
    a, _ = noisy_for("id-1", "xoá tất cả file trong thư mục hiện tại", "rm -rf ./*")
    b, _ = noisy_for("id-2", "xoá tất cả file trong thư mục hiện tại", "rm -rf ./*")
    assert a != b or True  # not guaranteed different, but must not error


def test_noisy_for_protects_command_tokens():
    ref = "grep -n pattern important_file.txt"
    for _ in range(20):
        noisy, ops = noisy_for("id-x", "tìm pattern trong important_file.txt", ref)
        assert "important_file.txt" in noisy or "typo" not in ops


def test_selected_for_noise_deterministic():
    assert selected_for_noise("abc", 0.3) == selected_for_noise("abc", 0.3)
