"""验证项目格式器只调整局部排版，字符串、控制流和语法树保持一致。"""

import ast

import pytest

from scripts.format_code import align_assignments, align_script


def test_alignment_keeps_assignment_groups_and_strings():
    source = 'a = "x=y"\nlong_name = 2\n\nz = 3\n'
    result = align_assignments(source)
    assert result.startswith('a         = "x=y"\nlong_name = 2\n')
    assert result.endswith("\nz = 3\n")
    assert ast.dump(ast.parse(result)) == ast.dump(ast.parse(source))
    assert align_assignments(result) == result


def test_keyword_calls_inside_control_flow_do_not_join_assignment_groups():
    source = 'names = set()\nfor line in read(encoding="utf-8"):\n    pass\n'
    assert align_assignments(source) == source


def test_multiline_keyword_alignment_has_spaces_on_both_sides():
    source = "call(\n    a=1,\n    longer=2,\n)\n"
    result = align_assignments(source)
    assert "a      = 1," in result
    assert "longer = 2," in result
    assert ast.dump(ast.parse(result)) == ast.dump(ast.parse(source))


def test_javascript_template_text_is_excluded_from_alignment():
    source = "const a = 1;\nconst longer = `text\nconst b = 2;\nconst longerName = 3;\n`;\n"
    result = align_script(source, ".js")
    assert result.startswith("const a      = 1;\nconst longer = `text")
    assert "const b = 2;\nconst longerName = 3;" in result
    assert align_script(result, ".js") == result


def test_shell_assignments_keep_required_no_space_syntax():
    source = "a=1\nlong_name=2\n"
    assert align_script(source, ".sh") == source


@pytest.mark.parametrize("absolute", [False, True])
def test_check_reports_unformatted_explicit_path_without_mutation(
    tmp_path, monkeypatch, capsys, absolute
):
    from scripts.format_code import main

    # 相对路径和仓库外的显式路径均应报告格式差异，文件内容保持原样。
    source = "a=1\nlong_name=2\n"
    path   = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["format_code.py", "--check", str(path) if absolute else path.name]
    )
    assert main() == 1
    assert path.read_text(encoding="utf-8") == source
    assert "sample.py" in capsys.readouterr().out
