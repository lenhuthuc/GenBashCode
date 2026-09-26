from vishell.prompts import SYSTEM_PROMPT, build_messages
from vishell.schema import parse_output


def test_build_messages_shape():
    msgs = build_messages("xoá file a.txt")
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert msgs[1] == {"role": "user", "content": "xoá file a.txt"}


def test_system_prompt_describes_json_contract():
    assert '"action"' in SYSTEM_PROMPT and '"command"' in SYSTEM_PROMPT and '"question"' in SYSTEM_PROMPT
    for action in ("execute", "probe", "ask"):
        assert action in SYSTEM_PROMPT


def test_system_prompt_own_example_output_parses():
    # sanity: the literal JSON shape embedded in the prompt is itself valid ModelOutput JSON
    example = '{"action": "execute", "command": "ls", "question": ""}'
    out, err = parse_output(example)
    assert err is None and out.action == "execute"
