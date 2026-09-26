import json

from vishell.schema import ModelOutput, parse_output


def test_parse_output_clean_json():
    out, err = parse_output('{"action": "probe", "command": "ls", "question": ""}')
    assert err is None
    assert out.action == "probe" and out.command == "ls"


def test_parse_output_with_surrounding_prose():
    text = 'Here is the answer: {"action": "ask", "command": "", "question": "?"} thanks'
    out, err = parse_output(text)
    assert err is None and out.action == "ask"


def test_parse_output_picks_first_valid_object_when_earlier_brace_is_garbage():
    text = '{not json} {"action": "execute", "command": "ls", "question": ""}'
    out, err = parse_output(text)
    assert err is None and out.command == "ls"


def test_parse_output_no_braces_at_all():
    out, err = parse_output("no json here")
    assert out is None and err


def test_parse_output_rejects_wrong_schema():
    out, err = parse_output('{"action": "not_a_real_action"}')
    assert out is None and err


def test_parse_output_defaults_command_and_question_to_empty():
    out, _ = parse_output('{"action": "ask"}')
    assert out.command == "" and out.question == ""


def test_model_output_roundtrip():
    o = ModelOutput(action="execute", command="rm -rf x", question="")
    parsed, err = parse_output(o.model_dump_json())
    assert err is None and parsed == o
