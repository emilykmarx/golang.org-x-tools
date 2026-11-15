#!/usr/bin/env python3
"""List every config key documented in Prometheus's configuration.md.

Each `### `<name>`` section defines a block in its (last) code block. References
to other blocks, such as `[ <http_config> ]`, `[ - <scrape_config> ... ]` or
`[ tsdb: <tsdb> ]`, are expanded in place, starting from the top-level config
under "## Configuration file". A key therefore appears once per place it can be
set, e.g. tls_config.ca once under every block that includes <tls_config>.

User-chosen map keys (`<string>:`, `<labelname>:`) are not keys themselves; they
appear in paths as written in the docs, e.g. scrape_configs.http_headers.<string>.values.

Only keys that take a value directly are listed; keys that just group other keys
(tls_config, basic_auth, kubernetes_sd_configs, ...) are not.

Output: one line per key, "<dot.separated.path>\t<type>", where <type> is the
documented type, or "map" for maps with user-chosen keys.

The same keys are also written as nested YAML with {} as each value, in the
format of dump_params' params.yaml (sorted keys, list levels not shown). A key
that is both listed and has listed children (http_headers, whose children are
<string>.values etc.) is a mapping of those children.

Usage: list_config_keys.py [configuration.md] [output_file] [yaml_output_file]
"""

import argparse
import re

import yaml

DEFAULT_DOC = "/home/emily/projects/config_tracing/prometheus/docs/configuration/configuration.md"

SECTION = re.compile(r"^### `<([a-z0-9_]+)>`")
# A key, optionally with its value: "key: value", "key : value" or "key <value>".
KEY = re.compile(r"^([a-z_][a-z0-9_]*)\s*(?::\s*(.*)|\s+(<.*))?$")
DYNAMIC_KEY = re.compile(r"^(<[a-z_]+>):\s*(.*)$")
REF = re.compile(r"^<([a-z0-9_]+)>(?:\s*\.\.\.)?$")
# Leading indentation, then the doc's optional-brackets and list-item markers.
PREFIX = re.compile(r"^( *)((?:\[ *)?)((?:- +)?)")


def parse_sections(doc):
    """Return {section name: code block lines}; the top level is under ""."""
    sections, name, block, in_block = {}, None, [], False
    for line in open(doc):
        line = line.rstrip("\n")
        if line.startswith("```"):
            in_block = not in_block
            if in_block:
                block = []
            elif name is not None:
                sections[name] = block  # last block in the section wins
            continue
        if in_block:
            block.append(line)
        elif line.startswith("## Configuration file"):
            name = ""
        elif m := SECTION.match(line):
            name = m.group(1)
        elif line.startswith("## "):
            name = None
    return sections


def parse_line(line):
    """Return (indent, text column, is list item, text) or None for comments/blank lines.

    Nesting follows the indentation before any "[", so "  [ a: <int> ]" and
    "  b:" are siblings. Keys continuing a list item ("[ - name: ..." then
    "    values: ...") line up with the item's text column instead.
    """
    if re.match(r"^\s*#", line) or not line.strip():
        return None
    m = PREFIX.match(line)
    assert m  # every part of PREFIX is optional
    indent, text_col, is_item = len(m.group(1)), m.end(), bool(m.group(3))
    text = line[m.end():].strip()
    text = re.sub(r"\s*\|\s*default.*$", "", text)   # "| default = ..."
    while text.endswith("]") and text.count("]") > text.count("["):
        text = text[:-1].rstrip()                     # closing optional-brackets
    return indent, text_col, is_item, text


def mark_child(entry, value_type=None):
    """Record that a key's own value was seen: child keys, or a bare value type."""
    if entry is None:
        return
    if value_type is None:
        if entry[1] not in ("block", "map"):
            entry[1] = "block"
        entry[2] = True
    elif not entry[2]:
        entry[1] = value_type


def expand(sections, name, prefix, out, owner=None, seen=()):
    """Append [path, type, has_child_keys] entries for block `name` under `prefix`."""
    if name in seen:
        raise ValueError(f"cycle through <{name}>")
    seen = seen + (name,)
    stack = [(-1, prefix, owner)]  # (indent, path, entry of the key owning the path)
    item = None                    # (text column, indent) of the current list item
    prev_entry = None              # entry of the previous line's key, if it had no value
    for raw in sections[name]:
        parsed = parse_line(raw)
        if parsed is None:
            continue
        indent, text_col, is_item, text = parsed
        if item and not is_item and text_col == item[0] and indent > item[1]:
            indent = item[1]  # continuation of a list item: sibling of its first key
        item = (text_col, indent) if is_item else (item if item and indent > item[1] else None)

        is_key = bool(KEY.match(text) or DYNAMIC_KEY.match(text))
        if not is_key and prev_entry is not None:
            # "tags_filter:" followed by "[ - <string> ]" at the same indent.
            parent, entry = prev_entry[0], prev_entry
        else:
            while stack[-1][0] >= indent:
                stack.pop()
            _, parent, entry = stack[-1]
        prev_entry = None

        if m := REF.match(text):
            if m.group(1) in sections:
                mark_child(entry)
                expand(sections, m.group(1), parent, out, entry, seen)
            else:
                mark_child(entry, text)  # e.g. rule_files: [ - <filepath_glob> ... ]
            continue
        if m := DYNAMIC_KEY.match(text):
            # User-chosen key, e.g. external_labels: [ <labelname>: <labelvalue> ... ]
            if entry is not None:
                entry[1], entry[2] = "map", True
            stack.append((indent, parent + [m.group(1)], None))
            continue
        m = KEY.match(text)
        if not m:
            mark_child(entry, text)  # a bare value, e.g. "<string>, [...]"
            continue
        mark_child(entry)
        key, value = m.group(1), (m.group(2) or m.group(3) or "").strip()
        new = [parent + [key], "block", False]
        out.append(new)
        ref = REF.match(value)
        if ref and ref.group(1) in sections:
            expand(sections, ref.group(1), new[0], out, new, seen)
        elif value:
            new[1] = value
        else:
            stack.append((indent, new[0], new))
            prev_entry = new


def main():
    parser = argparse.ArgumentParser(description="List every config key documented in configuration.md.")
    parser.add_argument("doc", nargs="?", default=DEFAULT_DOC)
    parser.add_argument("output", nargs="?", default="prometheus_config_keys.txt")
    parser.add_argument("yaml_output", nargs="?", default="prometheus_config_keys.yaml")
    args = parser.parse_args()

    sections = parse_sections(args.doc)
    out = []
    expand(sections, "", [], out)

    # Only keys that take a value directly; "block" keys just group other keys.
    keys = [(path, typ) for path, typ, _ in out if typ != "block"]
    with open(args.output, "w") as f:
        for path, typ in keys:
            f.write(f"{'.'.join(path)}\t{typ}\n")

    tree = {}
    for path, _ in keys:
        node = tree
        for part in path:
            node = node.setdefault(part, {})
    with open(args.yaml_output, "w") as f:
        yaml.safe_dump(tree, f, indent=4, default_flow_style=False)
    print(f"{len(keys)} keys -> {args.output}, {args.yaml_output}")


if __name__ == "__main__":
    main()
