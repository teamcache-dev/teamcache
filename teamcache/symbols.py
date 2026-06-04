from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .files import static_summary

_PARSERS: dict[str, Any] = {}


def extract_symbols(path: Path, language: str, text: str) -> dict[str, Any]:
    symbols = _empty_symbols()
    parser = _get_parser(language)
    if parser is None:
        return _fallback_symbols(path, language, text)

    source = text.encode("utf-8", errors="ignore")
    try:
        tree = parser.parse(source)
    except Exception:  # noqa: BLE001 - optional parser support must degrade gracefully.
        return _fallback_symbols(path, language, text)

    root = tree.root_node
    if language == "python":
        _extract_python(root, source, symbols)
    elif language in {"javascript", "typescript"}:
        _extract_js_ts(root, source, symbols)
    elif language == "go":
        _extract_go(root, source, symbols)
    elif language == "java":
        _extract_java(root, source, symbols)
    elif language == "rust":
        _extract_rust(root, source, symbols)
    elif language == "csharp":
        _extract_csharp(root, source, symbols)
    elif language == "ruby":
        _extract_ruby(root, source, symbols)
    elif language == "kotlin":
        _extract_kotlin(root, source, symbols)
    elif language == "php":
        _extract_php(root, source, symbols)
    elif language in {"cpp", "c"}:
        _extract_cpp(root, source, symbols)
    else:
        return _fallback_symbols(path, language, text)
    return _dedupe_symbols(symbols)


def write_symbol_object(symbols_root: Path, key: str, obj: dict[str, Any]) -> None:
    prefix_dir = symbols_root / key[:2]
    prefix_dir.mkdir(parents=True, exist_ok=True)
    final = prefix_dir / f"{key[:12]}_v1.json"
    tmp = final.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, final)


def summary_from_symbols(symbols: dict[str, Any], language: str) -> str:
    parts: list[str] = []
    _append(parts, "imports", _string_values(symbols.get("imports", [])))
    _append(parts, "exports", _string_values(symbols.get("exports", [])))
    _append(parts, "classes", _named_values(symbols.get("classes", [])))
    _append(parts, "functions", _named_values(symbols.get("functions", [])))
    _append(parts, "types", _named_values(symbols.get("types", [])))
    return " | ".join(parts) if parts else "no extractable structure"


def _empty_symbols() -> dict[str, Any]:
    return {"functions": [], "classes": [], "imports": [], "exports": []}


def _get_parser(language: str) -> Any | None:
    if language in _PARSERS:
        return _PARSERS[language]
    if language not in {
        "python",
        "javascript",
        "typescript",
        "go",
        "java",
        "rust",
        "csharp",
        "ruby",
        "kotlin",
        "php",
        "cpp",
        "c",
    }:
        return None
    try:
        from tree_sitter import Language, Parser
    except Exception:  # noqa: BLE001
        return None

    module_name, grammar_name = {
        "python": ("tree_sitter_python", "python"),
        "javascript": ("tree_sitter_javascript", "javascript"),
        "typescript": ("tree_sitter_typescript", "typescript"),
        "go": ("tree_sitter_go", "go"),
        "java": ("tree_sitter_java", "java"),
        "rust": ("tree_sitter_rust", "rust"),
        "csharp": ("tree_sitter_c_sharp", "csharp"),
        "ruby": ("tree_sitter_ruby", "ruby"),
        "kotlin": ("tree_sitter_kotlin", "kotlin"),
        "php": ("tree_sitter_php", "php"),
        "cpp": ("tree_sitter_cpp", "cpp"),
        "c": ("tree_sitter_cpp", "cpp"),
    }[language]
    try:
        module = __import__(module_name)
        if language == "typescript" and hasattr(module, "language_typescript"):
            grammar = module.language_typescript()
        elif language == "php" and hasattr(module, "language_php"):
            grammar = module.language_php()
        elif hasattr(module, "language"):
            grammar = module.language()
        else:
            return None

        ts_language = None
        for args in ((grammar,), (grammar, grammar_name)):
            try:
                ts_language = Language(*args)
                break
            except TypeError:
                continue
        if ts_language is None:
            ts_language = grammar

        parser = Parser()
        if hasattr(parser, "set_language"):
            parser.set_language(ts_language)
        else:
            parser.language = ts_language
        _PARSERS[language] = parser
        return parser
    except Exception:  # noqa: BLE001
        return None


def _extract_python(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for child in root.children:
        node = _decorated_definition(child)
        if node.type == "function_definition":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type == "class_definition":
            name = _node_name(node, source)
            if name:
                methods = [
                    {"name": method_name, "line": method.start_point[0] + 1, "end_line": method.end_point[0] + 1}
                    for method in _walk(node)
                    if method.type == "function_definition"
                    for method_name in [_node_name(method, source)]
                    if method_name
                ]
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": methods}
                )
        elif node.type in {"import_statement", "import_from_statement"}:
            import_name = _python_import_name(node, source)
            if import_name:
                symbols["imports"].append(import_name)


def _extract_js_ts(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for node in _walk(root):
        if node.type == "function_declaration":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type == "method_definition":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type == "lexical_declaration":
            for decl in _walk(node):
                if decl.type != "variable_declarator":
                    continue
                value = _field(decl, "value")
                if value is not None and value.type == "arrow_function":
                    name = _node_name(decl, source)
                    if name:
                        symbols["functions"].append(_function_obj(name, decl, source))
        elif node.type == "class_declaration":
            name = _node_name(node, source)
            if name:
                methods = [
                    {"name": method_name, "line": method.start_point[0] + 1, "end_line": method.end_point[0] + 1}
                    for method in _walk(node)
                    if method.type == "method_definition"
                    for method_name in [_node_name(method, source)]
                    if method_name
                ]
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": methods}
                )
        elif node.type == "import_statement":
            source_name = _string_child_value(node, source)
            if source_name:
                symbols["imports"].append(source_name)
        elif node.type in {"export_statement", "export_declaration"}:
            exported = _exported_name(node, source)
            if exported:
                symbols["exports"].append(exported)


def _extract_go(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    symbols["types"] = []
    for node in _walk(root):
        if node.type in {"function_declaration", "method_declaration"}:
            name = _node_name(node, source)
            if name:
                receiver = _go_receiver(node, source)
                display_name = f"{receiver}.{name}" if receiver else name
                symbols["functions"].append(_function_obj(display_name, node, source))
        elif node.type == "type_declaration":
            for child in _walk(node):
                if child.type == "type_spec":
                    value = _field(child, "type")
                    if value is not None and value.type == "struct_type":
                        name = _node_name(child, source)
                        if name:
                            symbols["types"].append(
                                {"name": name, "line": child.start_point[0] + 1}
                            )
        elif node.type == "import_declaration":
            for string_node in _nodes_of_type(node, {"interpreted_string_literal", "raw_string_literal"}):
                symbols["imports"].append(_strip_quotes(_text(string_node, source)))


def _extract_java(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for node in _walk(root):
        if node.type == "method_declaration":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type == "class_declaration":
            name = _node_name(node, source)
            if name:
                methods = [
                    {"name": method_name, "line": method.start_point[0] + 1, "end_line": method.end_point[0] + 1}
                    for method in _walk(node)
                    if method.type == "method_declaration"
                    for method_name in [_node_name(method, source)]
                    if method_name
                ]
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": methods}
                )
        elif node.type == "import_declaration":
            import_name = _java_import_name(node, source)
            if import_name:
                symbols["imports"].append(import_name)


def _extract_rust(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    symbols["types"] = []
    for node in _walk(root):
        if node.type == "function_item":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type in {"struct_item", "enum_item"}:
            name = _node_name(node, source)
            if name:
                symbols["types"].append({"name": name, "line": node.start_point[0] + 1})
        elif node.type == "use_declaration":
            use_text = _rust_use_name(node, source)
            if use_text:
                symbols["imports"].append(use_text)


def _extract_csharp(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for node in _walk(root):
        if node.type == "class_declaration":
            name = _node_name(node, source)
            if name:
                methods = [
                    {"name": method_name, "line": method.start_point[0] + 1, "end_line": method.end_point[0] + 1}
                    for method in _walk(node)
                    if method.type == "method_declaration"
                    for method_name in [_node_name(method, source)]
                    if method_name
                ]
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": methods}
                )
        elif node.type == "using_directive":
            import_name = _csharp_using_name(node, source)
            if import_name:
                symbols["imports"].append(import_name)


def _extract_ruby(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for node in _walk(root):
        if node.type == "class":
            name = _node_name(node, source)
            if name:
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": []}
                )
        elif node.type == "method":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type == "call":
            import_name = _ruby_require_name(node, source)
            if import_name:
                symbols["imports"].append(import_name)


def _extract_kotlin(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for node in _walk(root):
        if node.type in {"class_declaration", "object_declaration"}:
            name = _node_name(node, source)
            if name:
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": []}
                )
        elif node.type == "function_declaration":
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type in {"import_header", "import"}:
            import_name = _kotlin_import_name(node, source)
            if import_name:
                symbols["imports"].append(import_name)


def _extract_php(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    for node in _walk(root):
        if node.type in {"class_declaration", "interface_declaration"}:
            name = _node_name(node, source)
            if name:
                symbols["classes"].append(
                    {"name": name, "line": node.start_point[0] + 1, "methods": []}
                )
        elif node.type in {"function_definition", "method_declaration"}:
            name = _node_name(node, source)
            if name:
                symbols["functions"].append(_function_obj(name, node, source))
        elif node.type == "namespace_use_declaration":
            import_names = _php_namespace_use_names(node, source)
            if import_names:
                symbols["imports"].extend(import_names)


def _extract_cpp(root: Any, source: bytes, symbols: dict[str, Any]) -> None:
    """Extract symbols from C and C++ source files.

    Handles:
    - #include directives  → imports
    - using declarations   → imports
    - class/struct/union   → classes (with methods from field_declaration nodes)
    - function_definition  → functions (including template and scoped like Foo::bar)
    """
    for node in _walk(root):
        if node.type == "preproc_include":
            include_name = _cpp_include_name(node, source)
            if include_name:
                symbols["imports"].append(include_name)
        elif node.type == "using_declaration":
            using_name = _cpp_using_name(node, source)
            if using_name:
                symbols["imports"].append(using_name)
        elif node.type in {"class_specifier", "struct_specifier", "union_specifier"}:
            name_node = node.child_by_field_name("name")
            if name_node:
                cname = _text(name_node, source).strip()
                if cname:
                    methods = [
                        {
                            "name": mname,
                            "line": fn_decl.start_point[0] + 1,
                            "end_line": fn_decl.end_point[0] + 1,
                        }
                        for child in _walk(node)
                        if child.type == "field_declaration"
                        for fn_decl in child.children
                        if fn_decl.type == "function_declarator"
                        for d in [fn_decl.child_by_field_name("declarator")]
                        if d is not None
                        for mname in [_text(d, source).strip()]
                        if mname
                    ]
                    symbols["classes"].append(
                        {"name": cname, "line": node.start_point[0] + 1, "methods": methods}
                    )
        elif node.type == "function_definition":
            fname = _cpp_function_name(node, source)
            if fname:
                symbols["functions"].append(_function_obj(fname, node, source))


def _fallback_symbols(path: Path, language: str, text: str) -> dict[str, Any]:
    summary = static_summary(path, language, text)
    symbols = _empty_symbols()
    for label, values in re.findall(r"(\w+): ([^|]+)", summary):
        items = [item.strip() for item in values.split(",") if item.strip()]
        if label == "imports":
            symbols["imports"].extend(items)
        elif label == "exports":
            symbols["exports"].extend(items)
        elif label == "classes":
            symbols["classes"].extend({"name": item, "line": None, "methods": []} for item in items)
        elif label in {"functions", "symbols"}:
            symbols["functions"].extend(
                {"name": item, "line": None, "signature": item} for item in items
            )
        elif label == "types":
            symbols.setdefault("types", []).extend({"name": item, "line": None} for item in items)
    return _dedupe_symbols(symbols)


def _function_obj(name: str, node: Any, source: bytes) -> dict[str, Any]:
    call_types = {
        "call", "call_expression", "method_invocation",
        "invocation_expression", "function_call_expression",
    }
    call_field_names = ("function", "method", "name", "expression",
                        "calleeExpression")
    seen: set[str] = set()
    calls: list[str] = []
    for child in _walk(node):
        if child.type in call_types:
            for field_name in call_field_names:
                callee = child.child_by_field_name(field_name)
                if callee:
                    raw = _text(callee, source).strip()
                    # take the last dotted component: "self.db.query" -> "query"
                    part = raw.split(".")[-1].split("(")[0].strip()
                    if part and part.isidentifier() and part not in seen:
                        seen.add(part)
                        calls.append(part)
                    break
    return {
        "name": name,
        "line": node.start_point[0] + 1,
        "end_line": node.end_point[0] + 1,
        "signature": _first_line(node, source),
        "calls": calls,
    }


def _node_name(node: Any, source: bytes) -> str | None:
    named = _field(node, "name")
    if named is not None:
        return _text(named, source).strip()
    declarator = _field(node, "declarator")
    if declarator is not None:
        name = _node_name(declarator, source)
        if name:
            return name
    for child in node.children:
        if child.type in {
            "identifier",
            "property_identifier",
            "type_identifier",
            "constant",
            "simple_identifier",
            "name",
        }:
            return _text(child, source).strip()
    return None


def _python_import_name(node: Any, source: bytes) -> str | None:
    text = _text(node, source).strip()
    from_match = re.match(r"from\s+([.\w]+)\s+import", text)
    if from_match:
        return from_match.group(1)
    import_match = re.match(r"import\s+([^,\s]+)", text)
    if import_match:
        return import_match.group(1)
    for child in node.children:
        if child.type in {"dotted_name", "identifier"}:
            return _text(child, source).split(",")[0].strip()
    return None


def _java_import_name(node: Any, source: bytes) -> str | None:
    text = _text(node, source).strip()
    match = re.match(r"import\s+(?:static\s+)?([^;]+);?", text)
    return match.group(1).strip() if match else None


def _rust_use_name(node: Any, source: bytes) -> str | None:
    text = _text(node, source).strip()
    match = re.match(r"use\s+([^;]+);?", text)
    return match.group(1).strip() if match else None


def _csharp_using_name(node: Any, source: bytes) -> str | None:
    text = _text(node, source).strip()
    match = re.match(r"using\s+(?:static\s+)?([^;=]+);?", text)
    return match.group(1).strip() if match else None


def _ruby_require_name(node: Any, source: bytes) -> str | None:
    text = _text(node, source).strip()
    match = re.match(r"require(?:_relative)?\s*\(?\s*[\"']([^\"']+)[\"']", text)
    return match.group(1).strip() if match else None


def _kotlin_import_name(node: Any, source: bytes) -> str | None:
    text = _text(node, source).strip()
    match = re.match(r"import\s+(.+?)(?:\s+as\s+\w+)?$", text)
    return match.group(1).strip() if match else None


def _php_namespace_use_names(node: Any, source: bytes) -> list[str]:
    text = _text(node, source).strip()
    text = re.sub(r"^use\s+", "", text)
    text = text.rstrip(";")
    imports: list[str] = []
    for item in text.split(","):
        cleaned = re.sub(r"\s+as\s+\w+$", "", item.strip(), flags=re.IGNORECASE)
        if cleaned:
            imports.append(cleaned)
    return imports


def _cpp_include_name(node: Any, source: bytes) -> str | None:
    """Extract the header name from a #include directive.

    Returns the bare name without angle brackets or quotes,
    e.g. 'iostream' or 'mylib/utils.h'.
    """
    text = _text(node, source).strip()
    match = re.search(r"<([^>]+)>|\"([^\"]+)\"", text)
    if match:
        return (match.group(1) or match.group(2)).strip()
    return None


def _cpp_using_name(node: Any, source: bytes) -> str | None:
    """Extract the identifier from a using declaration/directive.

    Strips 'using namespace ' / 'using ' prefix and trailing semicolons.
    e.g. 'using namespace std;'  -> 'std'
         'using MyApp::Helper;' -> 'MyApp::Helper'
    """
    text = _text(node, source).strip().rstrip(";")
    match = re.match(r"using\s+(?:namespace\s+)?(.+)", text)
    return match.group(1).strip() if match else None


def _cpp_function_name(node: Any, source: bytes) -> str | None:
    """Extract the function name from a function_definition node.

    Handles plain functions, scoped definitions (Foo::bar),
    destructors (~Foo), and template functions.
    Walks into nested declarators to find the innermost function_declarator.
    """
    decl = node.child_by_field_name("declarator")
    if decl is None:
        return None
    # template_declaration wraps an inner function_definition; skip this level
    if decl.type == "template_declaration":
        return None
    # Find the innermost function_declarator (may be nested inside
    # pointer_declarator, reference_declarator, etc.)
    fn_decl = (
        decl
        if decl.type == "function_declarator"
        else next(
            (c for c in _walk(decl) if c.type == "function_declarator"),
            None,
        )
    )
    if fn_decl is None:
        return None
    name_node = fn_decl.child_by_field_name("declarator")
    if name_node is None:
        return None
    return _text(name_node, source).strip() or None


def _go_receiver(node: Any, source: bytes) -> str | None:
    receiver = _field(node, "receiver")
    if receiver is None:
        return None
    identifiers = [_text(child, source).strip("* ") for child in _walk(receiver) if child.type == "type_identifier"]
    return identifiers[-1] if identifiers else None


def _exported_name(node: Any, source: bytes) -> str | None:
    for child in _walk(node):
        if child.type in {
            "function_declaration",
            "class_declaration",
            "lexical_declaration",
            "variable_declarator",
        }:
            name = _node_name(child, source)
            if name:
                return name
    match = re.search(r"export\s+\{?\s*(\w+)", _text(node, source))
    return match.group(1) if match else None


def _string_child_value(node: Any, source: bytes) -> str | None:
    source_field = _field(node, "source")
    if source_field is not None:
        return _strip_quotes(_text(source_field, source))
    for child in _walk(node):
        if child.type in {"string", "string_fragment"}:
            value = _strip_quotes(_text(child, source))
            if value:
                return value
    return None


def _field(node: Any, field_name: str) -> Any | None:
    try:
        return node.child_by_field_name(field_name)
    except Exception:  # noqa: BLE001
        return None


def _decorated_definition(node: Any) -> Any:
    if node.type != "decorated_definition":
        return node
    for child in node.children:
        if child.type.endswith("_definition"):
            return child
    return node


def _walk(node: Any) -> list[Any]:
    nodes = [node]
    for child in node.children:
        nodes.extend(_walk(child))
    return nodes


def _nodes_of_type(node: Any, types: set[str]) -> list[Any]:
    return [child for child in _walk(node) if child.type in types]


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _first_line(node: Any, source: bytes) -> str:
    end = source.find(b"\n", node.start_byte, node.end_byte)
    if end == -1:
        end = node.end_byte
    return source[node.start_byte:end].decode("utf-8", errors="ignore").strip()


def _strip_quotes(value: str) -> str:
    return value.strip().strip("\"'`")


def _append(parts: list[str], label: str, values: list[str]) -> None:
    if values:
        parts.append(f"{label}: {', '.join(values)}")


def _string_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values if isinstance(values, list) else []:
        if value:
            result.append(str(value))
    return _unique(result)


def _named_values(values: Any) -> list[str]:
    result: list[str] = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, dict) and value.get("name"):
            result.append(str(value["name"]))
        elif value:
            result.append(str(value))
    return _unique(result)


def _dedupe_symbols(symbols: dict[str, Any]) -> dict[str, Any]:
    result = _empty_symbols()
    if "types" in symbols:
        result["types"] = []
    for key in result:
        seen: set[str] = set()
        for item in symbols.get(key, []):
            marker = item.get("name") if isinstance(item, dict) else item
            if marker is None:
                marker = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
            marker = str(marker)
            if marker in seen:
                continue
            seen.add(marker)
            result[key].append(item)
    return result


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
