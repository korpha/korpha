"""Tests for the post-write delta lint module."""
from __future__ import annotations

from korpha.skills.delta_lint import lint_text


# ---- python ----


def test_python_clean_returns_ok() -> None:
    src = "def hello():\n    return 1\n"
    r = lint_text(src, suffix=".py", filename="test.py")
    assert r.ok is True
    assert r.errors == []


def test_python_syntax_error_caught() -> None:
    src = "def hello(:\n    return 1\n"
    r = lint_text(src, suffix=".py", filename="bad.py")
    assert r.ok is False
    assert len(r.errors) == 1
    err = r.errors[0]
    assert err.file == "bad.py"
    assert err.line == 1
    assert "SyntaxError" in err.msg


def test_python_unclosed_bracket() -> None:
    src = "x = [1, 2, 3\n"
    r = lint_text(src, suffix=".py", filename="x.py")
    assert r.ok is False
    assert "SyntaxError" in r.errors[0].msg


def test_python_indent_error_caught() -> None:
    src = "def f():\nreturn 1\n"
    r = lint_text(src, suffix=".py", filename="i.py")
    assert r.ok is False


def test_python_render_includes_line() -> None:
    src = "def f(:\n"
    r = lint_text(src, suffix=".py", filename="x.py")
    rendered = r.render()
    assert "x.py" in rendered
    assert "line" in rendered


# ---- json ----


def test_json_clean() -> None:
    r = lint_text('{"a": 1}', suffix=".json", filename="t.json")
    assert r.ok is True


def test_json_trailing_comma_caught() -> None:
    r = lint_text('{"a": 1,}', suffix=".json", filename="t.json")
    assert r.ok is False
    assert "JSONDecodeError" in r.errors[0].msg
    assert r.errors[0].line is not None


def test_json_garbage_caught() -> None:
    r = lint_text("not json at all", suffix=".json", filename="t.json")
    assert r.ok is False


# ---- yaml ----


def test_yaml_clean() -> None:
    r = lint_text("a: 1\nb: 2\n", suffix=".yaml", filename="t.yaml")
    assert r.ok is True


def test_yaml_yml_extension_works() -> None:
    r = lint_text("a: 1\n", suffix=".yml", filename="t.yml")
    assert r.ok is True


def test_yaml_bad_caught() -> None:
    bad = "a: [1, 2,\nb: 3\n"  # unclosed list bracket
    r = lint_text(bad, suffix=".yaml", filename="t.yaml")
    assert r.ok is False
    assert "YAMLError" in r.errors[0].msg


# ---- toml ----


def test_toml_clean() -> None:
    r = lint_text('name = "foo"\n', suffix=".toml", filename="x.toml")
    assert r.ok is True


def test_toml_bad_caught() -> None:
    r = lint_text("name = ", suffix=".toml", filename="x.toml")
    assert r.ok is False
    assert "TOMLDecodeError" in r.errors[0].msg


# ---- bash ----


def test_bash_clean_returns_ok() -> None:
    r = lint_text("echo hello\n", suffix=".sh", filename="x.sh")
    # ok if bash is installed AND syntax clean. ok also if bash missing
    # (best-effort). Only fails if bash present and finds an error.
    assert r.ok is True


def test_bash_unclosed_brace_caught_when_bash_available() -> None:
    """If bash is on the system, ``bash -n`` should reject this. Skip
    the assertion (treat as N/A) when bash is missing."""
    import shutil
    if shutil.which("bash") is None:
        return
    bad = "if [ $x = 1 ]; then\n  echo missing fi\n"
    r = lint_text(bad, suffix=".sh", filename="x.sh")
    assert r.ok is False
    assert "syntax" in r.errors[0].msg.lower() or "error" in r.errors[0].msg.lower()


# ---- unknown / passthrough ----


def test_unknown_suffix_passes() -> None:
    """We don't reject formats we don't know."""
    r = lint_text("anything goes here", suffix=".xyz", filename="t.xyz")
    assert r.ok is True


def test_empty_text_python_ok() -> None:
    r = lint_text("", suffix=".py", filename="e.py")
    assert r.ok is True


def test_render_clean_message() -> None:
    r = lint_text("x = 1\n", suffix=".py", filename="ok.py")
    assert r.render() == "lint: clean"
