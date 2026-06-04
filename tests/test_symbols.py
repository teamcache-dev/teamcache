from __future__ import annotations

from pathlib import Path

import pytest

import teamcache.symbols as symbols_module
from teamcache.symbols import extract_symbols


def _extract(language: str, suffix: str, text: str) -> dict:
    symbols_module._PARSERS.pop(language, None)
    if symbols_module._get_parser(language) is None:
        pytest.skip(f"tree-sitter parser for {language} is not installed")
    return extract_symbols(Path(f"sample.{suffix}"), language, text)


def test_csharp_extracts_classes() -> None:
    symbols = _extract("csharp", "cs", "namespace Demo { class Greeter {} }\n")

    assert any(cls["name"] == "Greeter" for cls in symbols["classes"])


def test_csharp_extracts_class_methods() -> None:
    symbols = _extract(
        "csharp",
        "cs",
        "class Greeter {\n"
        "  public void SayHello() {}\n"
        "  private int Count() { return 1; }\n"
        "}\n",
    )

    greeter = next(cls for cls in symbols["classes"] if cls["name"] == "Greeter")
    method_names = {method["name"] for method in greeter["methods"]}
    assert method_names == {"SayHello", "Count"}


def test_csharp_extracts_using_directives() -> None:
    symbols = _extract(
        "csharp",
        "cs",
        "using System;\n"
        "using static System.Math;\n"
        "class App {}\n",
    )

    assert symbols["imports"] == ["System", "System.Math"]


def test_ruby_extracts_classes() -> None:
    symbols = _extract("ruby", "rb", "class Greeter\nend\n")

    assert any(cls["name"] == "Greeter" for cls in symbols["classes"])


def test_ruby_extracts_methods_as_functions() -> None:
    symbols = _extract("ruby", "rb", "def greet(name)\n  name\nend\n")

    assert any(fn["name"] == "greet" for fn in symbols["functions"])


def test_ruby_extracts_require_calls() -> None:
    symbols = _extract(
        "ruby",
        "rb",
        "require 'json'\n"
        "require_relative \"support/helper\"\n",
    )

    assert symbols["imports"] == ["json", "support/helper"]


def test_kotlin_extracts_classes_and_objects() -> None:
    symbols = _extract(
        "kotlin",
        "kt",
        "class Greeter\n"
        "object Registry\n",
    )

    class_names = {cls["name"] for cls in symbols["classes"]}
    assert class_names == {"Greeter", "Registry"}


def test_kotlin_extracts_functions() -> None:
    symbols = _extract("kotlin", "kt", "fun greet(name: String): String = name\n")

    assert any(fn["name"] == "greet" for fn in symbols["functions"])


def test_kotlin_extracts_import_headers() -> None:
    symbols = _extract(
        "kotlin",
        "kt",
        "import kotlin.collections.List\n"
        "import com.example.Widget as AppWidget\n",
    )

    assert symbols["imports"] == ["kotlin.collections.List", "com.example.Widget"]


def test_php_extracts_classes_and_interfaces() -> None:
    symbols = _extract(
        "php",
        "php",
        "<?php\n"
        "class Greeter {}\n"
        "interface Renderer {}\n",
    )

    class_names = {cls["name"] for cls in symbols["classes"]}
    assert class_names == {"Greeter", "Renderer"}


def test_php_extracts_functions_and_methods() -> None:
    symbols = _extract(
        "php",
        "php",
        "<?php\n"
        "function helper() {}\n"
        "class Greeter { public function greet() {} }\n",
    )

    function_names = {fn["name"] for fn in symbols["functions"]}
    assert function_names == {"helper", "greet"}


def test_php_extracts_namespace_use_declarations() -> None:
    symbols = _extract(
        "php",
        "php",
        "<?php\n"
        "use App\\Services\\Mailer;\n"
        "use App\\Models\\User as AccountUser, App\\Models\\Team;\n",
    )

    assert symbols["imports"] == [
        "App\\Services\\Mailer",
        "App\\Models\\User",
        "App\\Models\\Team",
    ]


def test_function_obj_has_end_line() -> None:
    sym = _extract("python", "py", "def foo(x):\n    return x + 1\n")
    fn = next(f for f in sym["functions"] if f["name"] == "foo")
    assert "end_line" in fn
    assert fn["end_line"] >= fn["line"]


def test_function_obj_has_calls() -> None:
    sym = _extract("python", "py",
        "def bar():\n    result = foo(1)\n    return result\n")
    fn = next(f for f in sym["functions"] if f["name"] == "bar")
    assert "calls" in fn
    assert "foo" in fn["calls"]


def test_calls_not_in_symbol_names() -> None:
    """outgoing_calls at top level must not pollute FTS symbol index."""
    from teamcache.db import _symbol_names
    symbols = {
        "functions": [
            {"name": "authenticate", "line": 1, "end_line": 5,
             "signature": "def authenticate():", "calls": ["db_query", "hash_pw"]},
        ],
        "classes": [], "imports": [], "exports": [],
    }
    result = _symbol_names(symbols)
    assert "db_query" not in result
    assert "hash_pw" not in result
    assert "authenticate" in result
