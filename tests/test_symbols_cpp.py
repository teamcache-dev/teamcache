"""Tests for C / C++ symbol extraction using tree-sitter-cpp."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_cpp", reason="tree-sitter-cpp not installed")

from pathlib import Path
from teamcache.symbols import extract_symbols


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract(suffix: str, source: str) -> dict:
    """Run extract_symbols on an in-memory source string."""
    from teamcache.constants import LANGUAGE_BY_SUFFIX
    language = LANGUAGE_BY_SUFFIX.get(f".{suffix}", "unknown")
    tmp = Path(f"fake.{suffix}")
    return extract_symbols(tmp, language, source)


def _cpp(source: str) -> dict:
    return _extract("cpp", source)


def _c(source: str) -> dict:
    return _extract("c", source)


# ---------------------------------------------------------------------------
# C++ — includes → imports
# ---------------------------------------------------------------------------

def test_cpp_system_include() -> None:
    sym = _cpp("#include <iostream>\n")
    assert "iostream" in sym["imports"]


def test_cpp_local_include() -> None:
    sym = _cpp('#include "mylib/utils.h"\n')
    assert "mylib/utils.h" in sym["imports"]


def test_cpp_multiple_includes() -> None:
    src = "#include <vector>\n#include <string>\n#include <algorithm>\n"
    sym = _cpp(src)
    assert "vector" in sym["imports"]
    assert "string" in sym["imports"]
    assert "algorithm" in sym["imports"]


# ---------------------------------------------------------------------------
# C++ — using declarations → imports
# ---------------------------------------------------------------------------

def test_cpp_using_namespace() -> None:
    sym = _cpp("using namespace std;\n")
    assert "std" in sym["imports"]


def test_cpp_using_scoped() -> None:
    sym = _cpp("using MyApp::Helper;\n")
    assert "MyApp::Helper" in sym["imports"]


# ---------------------------------------------------------------------------
# C++ — free functions → functions
# ---------------------------------------------------------------------------

def test_cpp_free_function() -> None:
    src = "int add(int a, int b) {\n    return a + b;\n}\n"
    sym = _cpp(src)
    names = [f["name"] for f in sym["functions"]]
    assert "add" in names


def test_cpp_function_has_line() -> None:
    src = "\n\nint helper(int x) {\n    return x;\n}\n"
    sym = _cpp(src)
    fn = next(f for f in sym["functions"] if f["name"] == "helper")
    assert fn["line"] == 3


def test_cpp_function_has_end_line() -> None:
    src = "int foo(int x) {\n    return x + 1;\n}\n"
    sym = _cpp(src)
    fn = next(f for f in sym["functions"] if f["name"] == "foo")
    assert "end_line" in fn
    assert fn["end_line"] >= fn["line"]


def test_cpp_function_has_calls() -> None:
    src = "int bar(int x) {\n    return helper(x);\n}\n"
    sym = _cpp(src)
    fn = next(f for f in sym["functions"] if f["name"] == "bar")
    assert "calls" in fn
    assert "helper" in fn["calls"]


def test_cpp_multiple_functions() -> None:
    src = (
        "void foo() {}\n"
        "int bar(int x) { return x; }\n"
        "double baz(double y) { return y * 2.0; }\n"
    )
    sym = _cpp(src)
    names = {f["name"] for f in sym["functions"]}
    assert {"foo", "bar", "baz"} <= names


# ---------------------------------------------------------------------------
# C++ — scoped function definitions (Foo::method)
# ---------------------------------------------------------------------------

def test_cpp_scoped_method_definition() -> None:
    src = (
        "class Greeter {};\n"
        "void Greeter::greet(const std::string& name) {\n"
        "    return;\n"
        "}\n"
    )
    sym = _cpp(src)
    names = [f["name"] for f in sym["functions"]]
    # tree-sitter represents Foo::bar as a qualified_identifier
    assert any("greet" in n for n in names)


# ---------------------------------------------------------------------------
# C++ — classes → classes
# ---------------------------------------------------------------------------

def test_cpp_class() -> None:
    src = "class MyClass {\npublic:\n    void method();\n};\n"
    sym = _cpp(src)
    class_names = [c["name"] for c in sym["classes"]]
    assert "MyClass" in class_names


def test_cpp_class_has_methods() -> None:
    src = (
        "class Greeter {\n"
        "public:\n"
        "    void greet(const std::string& name);\n"
        "    int count() const;\n"
        "};\n"
    )
    sym = _cpp(src)
    cls = next(c for c in sym["classes"] if c["name"] == "Greeter")
    method_names = [m["name"] for m in cls["methods"]]
    assert "greet" in method_names
    assert "count" in method_names


def test_cpp_class_method_has_line() -> None:
    src = (
        "class Foo {\n"          # line 1
        "public:\n"              # line 2
        "    void bar();\n"      # line 3
        "};\n"
    )
    sym = _cpp(src)
    cls = next(c for c in sym["classes"] if c["name"] == "Foo")
    method = next(m for m in cls["methods"] if m["name"] == "bar")
    assert method["line"] == 3


def test_cpp_struct() -> None:
    src = "struct Point {\n    int x;\n    int y;\n};\n"
    sym = _cpp(src)
    class_names = [c["name"] for c in sym["classes"]]
    assert "Point" in class_names


def test_cpp_no_duplicate_symbols() -> None:
    src = (
        "class Foo {\npublic:\n    void bar();\n};\n"
        "void Foo::bar() {}\n"
    )
    sym = _cpp(src)
    class_names = [c["name"] for c in sym["classes"]]
    assert class_names.count("Foo") == 1


# ---------------------------------------------------------------------------
# C++ — template functions
# ---------------------------------------------------------------------------

def test_cpp_template_function() -> None:
    src = "template<typename T>\nT add(T a, T b) { return a + b; }\n"
    sym = _cpp(src)
    # template functions are wrapped in template_declaration; we still
    # want to find the inner function_definition
    names = [f["name"] for f in sym["functions"]]
    assert "add" in names


# ---------------------------------------------------------------------------
# C — plain C source (.c suffix)
# ---------------------------------------------------------------------------

def test_c_free_function() -> None:
    src = "int add(int a, int b) {\n    return a + b;\n}\n"
    sym = _c(src)
    names = [f["name"] for f in sym["functions"]]
    assert "add" in names


def test_c_include() -> None:
    src = "#include <stdio.h>\nint main() { return 0; }\n"
    sym = _c(src)
    assert "stdio.h" in sym["imports"]


def test_c_struct() -> None:
    src = "struct Node {\n    int value;\n    struct Node* next;\n};\n"
    sym = _c(src)
    class_names = [c["name"] for c in sym["classes"]]
    assert "Node" in class_names


# ---------------------------------------------------------------------------
# General shape checks
# ---------------------------------------------------------------------------

def test_cpp_symbols_shape() -> None:
    src = (
        "#include <vector>\n"
        "class Foo {\npublic:\n    void bar();\n};\n"
        "void baz() {}\n"
    )
    sym = _cpp(src)
    assert "functions" in sym
    assert "classes" in sym
    assert "imports" in sym
    assert "exports" in sym


def test_cpp_empty_file_returns_empty_symbols() -> None:
    sym = _cpp("")
    assert sym["functions"] == []
    assert sym["classes"] == []
    assert sym["imports"] == []
