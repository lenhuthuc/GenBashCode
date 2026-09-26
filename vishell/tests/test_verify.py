import shutil

import pytest

from vishell.schema import Template
from vishell.verify import verify_all, verify_template

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker")


@pytest.fixture(scope="module")
def backend():
    from vishell.sandbox.backends import DockerBackend
    return DockerBackend(timeout=10)


GOOD_EXECUTE = Template(
    template_id="ex-good", source="original",
    params={"n": {"int": [1, 3]}},
    setup="for i in $(seq 1 {n}); do touch file_$i.txt; done",
    requests_vi=["xoá tất cả file .txt trong thư mục hiện tại"],
    expected_action="execute", reversible=True,
    reference_command="rm -f *.txt",
    wrong_commands=["true", "ls"],
    check_type="script", check_expected="[ -z \"$(ls *.txt 2>/dev/null)\" ]",
)

GOOD_PROBE = Template(
    template_id="pb-good", source="original",
    params={"n": {"int": [1, 5]}},
    setup="seq 1 {n} > f.txt",
    requests_vi=["đếm số dòng của file f.txt"],
    expected_action="probe", reversible=True,
    reference_command="wc -l < f.txt",
    wrong_commands=["echo 0"],
    # probe checks must read stdout (stdout_equals/contains/lines_set) — a "script"
    # check can only see filesystem state, which a read-only command never touches,
    # so it would trivially pass for any command including a no-op or a wrong one.
    check_type="stdout_equals", check_expected="{n}",
)

BAD_CHECK_TOO_LAX = Template(
    template_id="bad-lax", source="original",
    params={}, setup="echo hi > a.txt",
    requests_vi=["in ra nội dung file a.txt"],
    expected_action="probe", reversible=True,
    reference_command="cat a.txt", wrong_commands=["true"],
    check_type="stdout_contains", check_expected="",  # empty substring: 'true' passes too -> should fail (c)
)

ASK_IRREVERSIBLE = Template(
    template_id="ask-irr", source="original",
    params={}, setup="",
    requests_vi=["gửi request đến server production"],
    expected_action="ask", reversible=False, ask_reason="irreversible",
    clarify_question_vi="Bạn có chắc muốn gửi request này không?",
    reference_command="curl -X POST http://example.com/deploy",
    check_type="stdout_contains", check_expected="",
)

ASK_AMBIGUOUS = Template(
    template_id="ask-amb", source="original",
    params={}, setup="",
    requests_vi=["dọn dẹp thư mục này"],
    expected_action="ask", reversible=True, ask_reason="ambiguous",
    clarify_question_vi="Bạn muốn xoá những file nào cụ thể?",
    check_type="stdout_contains", check_expected="",
)


def test_good_execute_template_passes(backend):
    r = verify_template(GOOD_EXECUTE, backend)
    assert r.passed, r.reasons
    assert r.static_level == "R1"


def test_good_probe_template_passes(backend):
    r = verify_template(GOOD_PROBE, backend)
    assert r.passed, r.reasons
    assert r.static_level == "R0"


def test_too_lax_check_is_rejected(backend):
    r = verify_template(BAD_CHECK_TOO_LAX, backend)
    assert not r.passed
    assert any("no-op" in reason for reason in r.reasons)


def test_ask_irreversible_matches_classify(backend):
    r = verify_template(ASK_IRREVERSIBLE, backend)
    assert r.passed, r.reasons
    assert r.static_level == "R2"


def test_ask_ambiguous_only_needs_setup(backend):
    r = verify_template(ASK_AMBIGUOUS, backend)
    assert r.passed, r.reasons


def test_verify_all_aggregates(backend):
    report = verify_all([GOOD_EXECUTE, GOOD_PROBE, BAD_CHECK_TOO_LAX, ASK_IRREVERSIBLE, ASK_AMBIGUOUS], backend)
    assert report["n_templates"] == 5
    assert report["n_passed"] == 4
    assert report["n_failed"] == 1
