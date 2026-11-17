#!/usr/bin/env python3
"""List every config key documented in Alertmanager's docs/configuration.md and docs/https.md.

Adapted from ../../prometheus/documentedkeys/list_config_keys.py (the docs share Prometheus' format); see README.md
for what changed and why. Standard library only.

Each heading `<name>` (any level, e.g. ### `<route>`, #### `<oauth2>`) defines a block in its code block. References
to other blocks, such as `[ <tls_config> ]`, `[ - <receiver>, ... ]` or `http_config: <http_config>`, are expanded in
place, starting from a doc's top-level block. A key therefore appears once per place it can be set, e.g. tls_config.ca_file
once under every block that includes <tls_config>.

Output: one line per key, "<dot.separated.path>\t<type>", where <type> is the documented type, or "map" for maps
with user-chosen keys (`[ <labelname>: <labelvalue>, ... ]` or `{ <string>: <tmpl_string>, ... }`). Only keys that take
a value directly are listed; keys that just group other keys (tls_config, receivers, ...) are not, and neither is the
point where a recursive block stops being expanded (route.routes), which is printed to stderr instead.

Usage: list_config_keys.py [--docs-dir DIR] [--out-dir DIR]
"""

import argparse
import os
import re
import sys

DEFAULT_DOCS = "/home/emily/projects/config_tracing/alertmanager-paramtrack/docs"

# (doc, top-level heading => section name, section to start from, output file)
LISTS = [
    ("configuration.md", {"## File layout and global settings": ""}, "", "alertmanager_config_keys.txt"),
    ("https.md", {"## HTTP Traffic": "web"}, "web", "web_config_keys.txt"),
    ("https.md", {"## Gossip Traffic": "cluster_tls"}, "cluster_tls", "cluster_tls_config_keys.txt"),
]

# Blocks the docs reference but don't define as a `<name>` section.
ALIASES = {
    # "mute_time_intervals: [ - <mute_time_interval> ... ]" (deprecated): no section; the Go type MuteTimeInterval has
    # the same fields as TimeInterval (config/config.go).
    "mute_time_interval": "time_interval",
}
SUPPLEMENTS = {
    # "times: [ - <time_range> ...]" in <time_interval_spec>: defined only in prose ("They are specified like so:
    # times: - start_time: HH:MM / end_time: HH:MM").
    "time_range": ["start_time: HH:MM", "end_time: HH:MM"],
}

HEADING = re.compile(r"^(#{1,6}) +(.*?)\s*$")
SECTION = re.compile(r"^#{2,6} `<([a-z0-9_]+)>`$")
# A key, optionally with its value: "key: value", "key : value" or "key <value>". https.md has header names as keys
# (Content-Security-Policy), hence upper case and "-".
KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*(?::\s*(.*)|\s+(<.*))?$")
DYNAMIC_KEY = re.compile(r"^(<[a-z_]+>):\s*(.*)$")
REF = re.compile(r"^<([a-z0-9_]+)>(?:,?\s*\.\.\.)?$")
INLINE_MAP = re.compile(r"^\{\s*<[a-z_]+>:\s*<[a-z_]+>,?\s*\.\.\.\s*\}$")  # { <string>: <tmpl_string>, ... }
# Leading indentation, then the doc's optional-brackets and list-item markers.
PREFIX = re.compile(r"^( *)((?:\[ *)?)((?:- +)?)")


def parse_sections(doc, top):
    """Return {section name: code block lines}. A `<name>` heading, or one in `top`, starts a section; any other
    heading ends it. Its block is its only ```yaml block, else its only block (configuration.md's webhook section also
    has a JSON payload example; https.md's blocks are untagged)."""
    blocks = {}  # name => [(fence language, lines)]
    name, block, lang, in_block = None, [], "", False
    for line in open(doc):
        line = line.rstrip("\n")
        if line.startswith("```"):
            in_block = not in_block
            if in_block:
                block, lang = [], line[3:].strip()
            elif name is not None:
                blocks.setdefault(name, []).append((lang, block))
            continue
        if in_block:
            block.append(line)
        elif HEADING.match(line):
            m = SECTION.match(line)
            name = m.group(1) if m else top.get(line.strip())
    sections = {}
    for name, bs in blocks.items():
        yaml_blocks = [b for lang, b in bs if lang == "yaml"]
        chosen = yaml_blocks if yaml_blocks else [b for _, b in bs]
        if len(chosen) != 1:
            sys.exit(f"{doc}: section <{name}> has {len(chosen)} candidate code blocks")
        sections[name] = drop_multiline_defaults(chosen[0])
    return sections


def drop_multiline_defaults(lines):
    """Drop the continuation lines of a multi-line default value, e.g. pagerduty's
    "[ details: { <string>: <tmpl_string>, ... } | default = {" ... "} ]" (their lines would parse as keys)."""
    out, skipping = [], False
    for line in lines:
        if skipping:
            skipping = not line.strip().startswith("}")
            continue
        out.append(line)
        tail = re.split(r"default\s*=?", line, maxsplit=1)
        skipping = len(tail) == 2 and tail[1].count("{") > tail[1].count("}")
    return out


def parse_line(line):
    """Return (indent, text column, is list item, text) or None for comments/blank lines.

    Nesting follows the indentation before any "[", so "  [ a: <int> ]" and "  b:" are siblings. Keys continuing a
    list item ("- times:" then "  weekdays:") line up with the item's text column instead.
    """
    if re.match(r"^\s*#", line) or not line.strip():
        return None
    m = PREFIX.match(line)
    assert m  # every part of PREFIX is optional
    indent, text_col, is_item = len(m.group(1)), m.end(), bool(m.group(3))
    text = line[m.end():].strip()
    text = re.sub(r"(\s*\|\s*|\s+)default\b.*$", "", text)  # "| default = ...", also without "|"
    while text.endswith("]") and text.count("]") > text.count("["):
        text = text[:-1].rstrip()  # closing optional-brackets
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


def expand(sections, name, prefix, out, cuts, owner=None, seen=()):
    """Append [path, type, has_child_keys] entries for block `name` under `prefix`. A block that is already being
    expanded (an ancestor) isn't expanded again: its owner gets type "recursive:<name>" and is recorded in `cuts`."""
    if name in seen:
        owner[1], owner[2] = f"recursive:<{name}>", True
        cuts.append(".".join(owner[0]))
        return
    seen = seen + (name,)
    stack = [(-1, prefix, owner)]  # (indent, path, entry of the key owning the path)
    items = []                     # (text column, indent) of the enclosing list items
    prev_entry = None              # entry of the previous line's key, if it had no value
    for raw in sections[name]:
        parsed = parse_line(raw)
        if parsed is None:
            continue
        indent, text_col, is_item, text = parsed
        if not is_item:
            for i in range(len(items) - 1, -1, -1):
                if items[i][0] == text_col and indent > items[i][1]:
                    indent = items[i][1]  # continuation of a list item: sibling of its first key
                    break
        items = [it for it in items if it[1] < indent or (not is_item and it[1] == indent)]
        if is_item:
            items.append((text_col, indent))

        is_key = bool(KEY.match(text) or DYNAMIC_KEY.match(text))
        if not is_key and prev_entry is not None:
            # "times:" followed by "[ - <time_range> ...]" at the same indent.
            parent, entry = prev_entry[0], prev_entry
        else:
            while stack[-1][0] >= indent:
                stack.pop()
            _, parent, entry = stack[-1]
        prev_entry = None

        if m := REF.match(text):
            if (ref := ALIASES.get(m.group(1), m.group(1))) in sections:
                if ref not in seen:
                    mark_child(entry)
                expand(sections, ref, parent, out, cuts, entry, seen)
            else:
                mark_child(entry, text)  # e.g. matchers: [ - <matcher> ... ] (a string)
            continue
        if m := DYNAMIC_KEY.match(text):
            # User-chosen key, e.g. match: [ <labelname>: <labelvalue>, ... ]
            if entry is not None:
                entry[1], entry[2] = "map", True
            stack.append((indent, parent + [m.group(1)], None))
            continue
        m = KEY.match(text)
        if not m:
            mark_child(entry, text)  # a bare value, e.g. "- <string> ..."
            continue
        mark_child(entry)
        key, value = m.group(1), (m.group(2) or m.group(3) or "").strip()
        new = [parent + [key], "block", False]
        out.append(new)
        ref = REF.match(value)
        if ref and ALIASES.get(ref.group(1), ref.group(1)) in sections:
            expand(sections, ALIASES.get(ref.group(1), ref.group(1)), new[0], out, cuts, new, seen)
        elif INLINE_MAP.match(value):
            new[1] = "map"  # e.g. headers: { <string>: <tmpl_string>, ... }
        elif value:
            new[1] = value
        else:
            stack.append((indent, new[0], new))
            prev_entry = new


def main():
    parser = argparse.ArgumentParser(description="List every config key documented in Alertmanager's docs.")
    parser.add_argument("--docs-dir", default=DEFAULT_DOCS)
    parser.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = parser.parse_args()

    for doc, top, start, output in LISTS:
        sections = parse_sections(os.path.join(args.docs_dir, doc), top)
        sections.update(SUPPLEMENTS)
        out, cuts = [], []
        expand(sections, start, [], out, cuts)
        # Only keys that take a value directly; "block" keys just group other keys, "recursive:" ones are cut points.
        keys = [(path, typ) for path, typ, _ in out if typ != "block" and not typ.startswith("recursive:")]
        paths = [".".join(p) for p, _ in keys]
        dups = {p for p in paths if paths.count(p) > 1}
        if dups:
            sys.exit(f"{doc}: duplicate keys {sorted(dups)}")
        with open(os.path.join(args.out_dir, output), "w") as f:
            for path, typ in keys:
                f.write(f"{'.'.join(path)}\t{typ}\n")
        print(f"{doc}: {len(keys)} keys -> {output}" + (f"; recursion cut at {', '.join(cuts)}" if cuts else ""),
              file=sys.stderr)


if __name__ == "__main__":
    main()
