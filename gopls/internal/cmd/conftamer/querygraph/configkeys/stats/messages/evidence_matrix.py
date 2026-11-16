#!/usr/bin/env python3
"""A message x section matrix of a paramtrack run's evidence mappings, to look for missing mappings (possible false
negatives): an HTML page (interactive) and a markdown summary. LEGEND below says what's in it.

Inputs, all from the run directory:
- the messages: step 5's 5-matches/events.jsonl (chains.STEP), labelled as argmatch-labels' join.py labels them (as count_labels.py
  does), sent messages only (Request sent, Response sent), each label reduced to its path or route pattern (its
  api_id, method, paths and code fields dropped; chains.reduce_label) and the duplicates that leaves merged; with their
  chains and chain groups (chains.py);
- the sections: step 8's 8-stats/full_keys.csv (every in-scope full key, with its section);
- the concrete config's keys and values: step 4's configkeys.csv, and param_values.txt (the values searched for);
- unambiguous keys: those whose values are all unambiguous (TERMINOLOGY.md; stats.unambiguous_key, as stats.py's
  Unambiguous column), the values' ambiguity from step 8's 8-stats/values.csv;
- evidence: step 6's 6-label-values/joined.csv (message, matched value), and step 7's evidence rows
  (7-label-keys/label_keys.csv) for each value's full keys.

Labels are normalized and, on the HTML pages, collapsed:
- normalized: the parts of a label that differ between runs of the same testbed and config (e.g. pod name suffixes) are
  placeholders (argmatch-labels' join.py, NORMALIZE), so a label is the same message in every run;
- collapsed: messages of the same kind and chain group whose evidence mappings are identical (the same evidence full
  keys in every section) and not empty share one column (collapse_columns), e.g. Caddy's reads of each generated CA's Secrets. Only
  then: messages whose mappings differ in any section, and messages with no evidence, keep their own columns. A collapsed cell is anchored if any of
  its messages is; its tooltip names the message of each anchor value. The markdown summary and the pages' table view
  of anchored cells keep one row per message.

Usage: uv run --with pandas --with tabulate python evidence_matrix.py --run <paramtrack out dir> --out <prefix>
writes <prefix>.md, and a page per message kind: <prefix>-requests.html (Request sent) and <prefix>-responses.html
(Response sent)

With --received, instead the received-message matrices: received messages x sent messages, a page per received kind,
<prefix>-received-requests.html (Request received) and <prefix>-received-responses.html (Response received). Rows: the
received messages of that kind (from the same events, labelled and reduced the same way); columns: every sent message,
requests then responses; a cell is colored if the two messages' events share a context ID (RECEIVED_LEGEND).
"""

import argparse
import collections
import csv
import functools
import html
import json
import os
import sys

import pandas as pd

import chains  # (next to this file)
import layout  # (paramtrack's: where a run's step outputs are; chains puts paramtrack on the path)
import matches  # (next to this file)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stats  # noqa: E402  (configkeys/stats/stats.py: the concrete keys' values and full keys)

SENT = ["Request sent", "Response sent"]
RECEIVED = ["Request received", "Response received"]

# Both pages' legends (the sent-message and received-message matrices); the rules from join.py itself
LABELS_NOTE = ("Labels", "Message labels are normalized: parts that differ between runs of the same testbed and config "
                         "are replaced by placeholders, by these regular expressions (pattern => replacement), in order: "
                         + "; ".join(f"{rx.pattern} => {sub}" for rx, sub in chains.join.NORMALIZE)
                         + ". See NORMALIZE and normalize() in argmatch-labels' join.py.")


@functools.lru_cache
def normalization_examples(run):
    """One example per NORMALIZE rule, from the run's messages (chains.STEP's events): the first label (sorted) the
    rule itself changes (after the rules before it), as the matrix shows it (chains.reduce_label) before and after
    normalizing"""
    events = [json.loads(line) for line in open(f"{layout.step_dir(run, chains.STEP)}/events.jsonl") if line.strip()]
    raw = sorted(set(chains.join.message_labels(events, normalized=False).values()))
    rules = chains.join.NORMALIZE
    out = []

    def before(label, i):  # label after the rules before rule i
        for rx, sub in rules[:i]:
            label = rx.sub(sub, label)
        return label
    for i, (rx, sub) in enumerate(rules):
        label = next((label for label in raw if rx.sub(sub, before(label, i)) != before(label, i)), None)
        out.append(f"{rx.pattern}: " + (f"{chains.reduce_label(label)} => {chains.reduce_label(chains.join.normalize(label))}"
                                        if label else "no label in this run"))
    return out


def legend(entries, run):
    """A legend's entries, with the normalization examples (normalization_examples) after LABELS_NOTE"""
    out = []
    for e in entries:
        out.append(e)
        if e is LABELS_NOTE:
            out.append(("Normalization examples", "; ".join(normalization_examples(run))))
    return out


LEGEND = [
    LABELS_NOTE,
    ("Collapsed columns", "Messages of the same kind and chain group with identical evidence mappings (the same evidence "
                          "keys in every section) share a column, headed by the first one and the number merged; hover "
                          "the header for all of them. A cell is outlined if any of them anchors it, and its tooltip "
                          "names each anchor value's message. Messages whose mappings differ, and messages with no "
                          "evidence, keep their own columns. See collapse_columns() in "
                          "configkeys/stats/messages/evidence_matrix.py."),
    ("Terms", "as defined in configkeys/stats/TERMINOLOGY.md. UNAMBIGUOUS matches only: eligible keys, evidence "
              "keys and the keys of anchor values count only if they're Unambiguous."),
    ("Rows",
        "Every in-scope section, grouped by top-level key. Horizontal line between top-level keys."),
    ("Columns",
        "Observed sends: Path/pattern field of message label. Ordered by chain "  # noqa: ISC004
        "group: messages whose chains have exactly the same set of module "
        "functions. Vertical line between chain groups. (In this run, message labels were uniquely identified "
        "by path/pattern, with one exception, affecting two paths (/api/v1/pods and /api/v1/services), due to a "
        "labeling bug)"),
    ("Cell color",
        "White = The section has eligible keys, but none is evidence for this message. "
        "Blue = The section has evidence for this message; "
        "shade of blue = share of the section's eligible keys that are evidence for this message. "  # noqa: ISC004
        "Gray = The section has no eligible key (so cannot provide evidence for any message.)"),
    ("Orange-outlined cell", "The message label contains a config value, or vice versa. "  # noqa: ISC004
        "E.g. a token_url value http://host/ccfg-...-token-url contains the label "
        "/ccfg-...-token-url. (On hover, this is called an \"anchor\")"),
]


# The cells' blue ramp, for a share from 0 to 1 (palette.md's sequential steps 100..700)
RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab",
        "#184f95", "#104281", "#0d366b"]


def ramp_fill(share):
    """A cell's background for a share in (0, 1]"""
    return RAMP[min(len(RAMP) - 1, int(share * (len(RAMP) - 1) + 0.5))]


# The pages' styles (both matrices): light and dark modes, sticky row headers, angled column labels, lines between
# groups (td.c.g: a column group's first column; tr.sep: a row group's first row), outlined anchors (td.c.a)
CSS = """.viz-root{color-scheme:light;--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;
--text-secondary:#52514e;--muted:#898781;--grid:#e1e0d9;--zero:#fcfcfb;--no-eligible:#c3c2b7;--outline:#eb6834}
@media (prefers-color-scheme: dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#f0efec;--text-secondary:#c3c2b7;--grid:#2c2c2a;--zero:#1a1a19;
--no-eligible:#52514e;--outline:#d95926}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--grid:#2c2c2a;--zero:#1a1a19;--no-eligible:#52514e;--outline:#d95926}
body{margin:0}.viz-root{background:var(--page);color:var(--text-primary);font:13px system-ui,-apple-system,"Segoe UI",
sans-serif;padding:16px}
h1{font-size:18px;margin:0 0 8px}.legend{max-width:1100px;color:var(--text-secondary);line-height:1.45}
.legend b{color:var(--text-primary)}.swatch{display:inline-block;width:12px;height:12px;vertical-align:-2px;
border:1px solid var(--grid);margin:0 2px}
.wrap{overflow:auto;background:var(--surface-1);border:1px solid var(--grid);margin-top:12px}
table{border-collapse:collapse}  /* (a 1px surface border stands in for the gaps; the 2px lines win over it) */
th{font-weight:400;color:var(--text-secondary);font-size:11px}
th.top{text-align:left;vertical-align:top;color:var(--text-primary);padding:2px 6px 0 2px;white-space:nowrap;
width:110px;min-width:110px;position:sticky;left:0;z-index:2;background:var(--surface-1)}
th.col{height:270px;vertical-align:bottom;padding:0;position:relative;min-width:16px}
th.col div{position:absolute;bottom:4px;left:9px;transform-origin:0 100%;transform:rotate(-60deg);white-space:nowrap;
max-width:300px;overflow:hidden;text-overflow:ellipsis;text-align:left}  /* diagonal, up and to the right (60°: 16px columns put labels ~14px apart) */
.wrap table{margin-right:160px}  /* (room for the last labels, which extend past the last column) */
th.row{text-align:left;white-space:nowrap;max-width:360px;overflow:hidden;text-overflow:ellipsis;padding-right:6px;
position:sticky;left:118px;z-index:2;background:var(--surface-1)}
td.c{width:16px;height:14px;padding:0;border:1px solid var(--surface-1)}
td.c.g{border-left:2px solid var(--text-primary)}  /* a chain group's first column: a line on its left (in the body
   only: in the header it would cross the angled labels) */
td.c.a{outline:2px solid var(--outline);outline-offset:0;position:relative;z-index:1}
td.c:hover{outline:2px solid var(--muted);outline-offset:-2px}
tr.sep td,tr.sep th{border-top:2px solid var(--text-primary)}  /* a top-level key's first row: a line above */
#tip{position:fixed;z-index:10;pointer-events:none;background:var(--surface-1);color:var(--text-primary);border:1px solid
var(--grid);padding:6px 8px;max-width:520px;font-size:12px;line-height:1.4;display:none;box-shadow:0 2px 8px
rgba(0,0,0,.15)}#tip .v{font-weight:600}#tip .kv{padding-left:12px}#tip .s{color:var(--text-secondary)}
details{margin-top:12px;color:var(--text-secondary)}"""


def load(run):
    """Everything the matrix needs, from the run directory"""
    fkdf = pd.read_csv(f"{layout.step_dir(run, '8-stats')}/full_keys.csv", keep_default_na=False, dtype=str)
    section_of = dict(zip(fkdf.key, fkdf.section))  # in-scope full key => section
    section_keys = fkdf.groupby("section").key.apply(lambda ks: sorted(set(ks))).to_dict()  # section => its keys
    sections = sorted(set(fkdf.section), key=lambda s: (s.split(".")[0].split(" ")[0], s))

    # Unambiguous keys (matches.unambiguous_keys: stats.py's rule), with the concrete keys' values
    unamb_keys, covered_by, config, _ = matches.unambiguous_keys(run)
    searched = set(open(f"{layout.step_dir(run, '4-configkeys')}/param_values.txt").read().splitlines())

    # Eligible keys that are unambiguous, per section: set, with a value searched for; and the values of unambiguous
    # keys, with their sections (for anchors)
    eligible = collections.defaultdict(set)  # section => full keys
    value_sections = collections.defaultdict(set)  # value of an unambiguous key => its section
    for fk in unamb_keys:
        vs = stats.key_values(fk, covered_by, config)
        if any(v in searched for v in vs):
            eligible[section_of[fk]].add(fk)
        for v in vs:
            value_sections[v].add(section_of[fk])

    # Evidence: (kind, message) => section => its evidence full keys that are unambiguous
    needle_fks = matches.needle_full_keys(run)
    evidence = collections.defaultdict(lambda: collections.defaultdict(set))
    # ((kind, message), section) => evidence full key => its matched values
    via = collections.defaultdict(lambda: collections.defaultdict(set))
    for r in csv.DictReader(open(f"{layout.step_dir(run, '6-label-values')}/joined.csv", newline="")):
        if r["message_kind"] not in SENT:
            continue
        msg = (r["message_kind"], chains.reduce_label(r["message_label"]))
        for fk in needle_fks.get(r["needle"], ()):
            if fk in unamb_keys:
                evidence[msg][section_of[fk]].add(fk)
                via[(msg, section_of[fk])][fk].add(r["needle"])

    # Messages, ordered by chain group
    ch = chains.message_chains(run)  # (kind, reduced label) => chains, for every message of a sent kind
    msgs = sorted(k for k in ch if k[0] in SENT)
    order, group_of = by_chain_group(ch, msgs)

    # Anchors: (kind, message) => section => the values that anchor it
    anchors = collections.defaultdict(lambda: collections.defaultdict(set))
    for k in msgs:
        for v, secs in value_sections.items():
            if v in k[1] or k[1] in v:
                for s in secs:
                    anchors[k][s].add(v)
    return dict(sections=sections, section_keys=section_keys, eligible=eligible, evidence=evidence, via=via, msgs=order, group_of=group_of,
                anchors=anchors)


def by_chain_group(ch, msgs):
    """msgs ordered by chain group (messages whose chains have exactly the same set of module functions; the largest
    groups first), and each one's group number (from 1; 0 for a message with no module function on its chains)"""
    funcs = {k: chains.module_functions(ch.get(k, []))[0] for k in msgs}
    order, group_of = [], {}
    for i, g in enumerate(sorted(chains.groups_of(funcs), key=lambda g: (-len(g), sorted(g)))):
        for k in sorted(g):
            order.append(k)
            group_of[k] = i + 1
    for k in msgs:
        if k not in group_of:
            order.append(k)
            group_of[k] = 0
    return order, group_of


def cells(d):
    """One row per (message, section): its kind and recomputed confidence_eligible. Recomputed because paramtrack's version doesn't account for ambiguity."""
    rows = []
    for k in d["msgs"]:
        for s in d["sections"]:
            el, ev = d["eligible"].get(s, set()), d["evidence"][k].get(s, set())
            anchored = bool(d["anchors"][k].get(s))
            if not el:
                kind = "anchored, no eligible keys" if anchored else "no eligible keys"
                conf = None
            else:
                conf = len(ev & el) / len(el)
                kind = ("anchored, evidence" if conf else "anchored, no evidence (possible false negative)") \
                    if anchored else ("evidence" if conf else "no evidence")
            rows.append({"kind_msg": k[0], "message": k[1], "group": d["group_of"][k], "section": s,
                         "top": s.split(".")[0].split(" ")[0], "cell": kind, "confidence": conf,
                         "evidence_keys": len(ev & el), "eligible_keys": len(el),
                         "anchor_values": "; ".join(sorted(d["anchors"][k].get(s, ()))),
                         # one 'key = "value"' per evidence key (its matched values, if several, joined by ", ";
                         # quoted as JSON strings)
                         "evidence": [f"{fk} = {', '.join(json.dumps(v) for v in sorted(vs))}"
                                      for fk, vs in sorted(d["via"].get((k, s), {}).items())]})
    return pd.DataFrame(rows)


def write_md(df, d, run, path):
    L = [f"<!-- Generated by configkeys/stats/messages/evidence_matrix.py --run {run} -->",
         f"# Evidence mappings: messages x sections ({run})", "", "## Legend", ""]
    L += [f"- **{name}:** {text}" for name, text in legend(LEGEND, run)] + [""]
    tested = df[df.eligible_keys > 0].section.nunique()
    L += ["## Counts", "", f"- Messages (rows): {len(d['msgs'])}; in-scope sections (columns): "
          f"{len(d['sections'])}, {tested} with eligible keys", ""]
    order = ["anchored, evidence", "anchored, no evidence (possible false negative)", "anchored, no eligible keys",
             "evidence", "no evidence", "no eligible keys"]
    counts = df.cell.value_counts()
    L += [pd.DataFrame([(c, int(counts.get(c, 0))) for c in order], columns=["cell", "cells"]).to_markdown(index=False),
          ""]
    an = df[df.cell.str.startswith("anchored")].sort_values(["group", "message", "section"])
    L += ["## Anchored cells", "", f"{len(an)} cells, in {an.message.nunique()} messages:", "",
          an[["message", "group", "section", "anchor_values", "cell", "confidence", "evidence"]]
          .assign(evidence=lambda x: x.evidence.map("; ".join))
          .assign(confidence=lambda x: x.confidence.map(lambda c: "-" if pd.isna(c) else f"{c:.2f}"))
          .to_markdown(index=False), ""]
    sec = df.groupby("section").agg(eligible=("eligible_keys", "first"), msgs=("evidence_keys", lambda s: (s > 0).sum()))
    never = sec[(sec.eligible > 0) & (sec.msgs == 0)]
    L += ["## Sections with eligible keys but no evidence for any message", "",
          f"{len(never)} of {tested}:", ""] + [f"- `{s}` ({r.eligible} eligible keys)" for s, r in never.iterrows()]
    L += ["", f"## Sections with no eligible keys (gray columns): {(sec.eligible == 0).sum()}", ""]
    L += [f"- `{s}`" for s in sec[sec.eligible == 0].index] + [""]
    open(path, "w").write("\n".join(L))


def short_section(s):
    """A row label: the section without its top-level key (the first column names it) and with a shorter
    Unmarshaler, e.g. `alerting.alertmanagers.oauth2 @ .../common/config.OAuth2` => `alertmanagers.oauth2 @
    config.OAuth2`; `@ config.X` alone for the top-level key's own section"""
    parent, _, unm = s.partition(" @ ")
    unm = unm.rsplit("/", 1)[-1]
    if not parent:
        return f"(top level) @ {unm}"
    rest = parent.partition(".")[2]
    return f"{rest} @ {unm}" if rest else f"@ {unm}"


def collapse_columns(d, msgs):
    """msgs (in order) grouped into columns: lists of messages of the same chain group whose evidence mappings are
    identical (the same evidence full keys in every section) and not empty, in the order of each column's first
    message; a message with no evidence has its own column"""
    cols = {}
    for k in msgs:
        ev = frozenset((s, frozenset(fks)) for s, fks in d["evidence"][k].items() if fks)
        cols.setdefault((d["group_of"][k], ev) if ev else k, []).append(k)
    return list(cols.values())


def write_html(df, d, run, path, kind):
    """The matrix as a table: each cell's color is its confidence (blue ramp, palette.md's sequential steps), gray if
    the section has no eligible keys, with an outline if anchored; a tooltip per cell. Light and dark modes. Only the
    messages of the given kind (Request sent or Response sent)."""
    df = df[df.kind_msg == kind]
    msgs = [k for k in d["msgs"] if k[0] == kind]
    what = "requests" if kind == "Request sent" else "responses"
    ramp = RAMP

    def fill(c):
        if c is None or pd.isna(c):
            return "var(--no-eligible)"
        if c == 0:
            return "var(--zero)"
        return ramp_fill(c)

    def top(s):
        return s.split(".")[0].split(" ")[0]

    span = collections.Counter(top(s) for s in d["sections"])
    by = {(r.message, r.kind_msg, r.section): r for r in df.itertuples()}
    esc = html.escape
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Evidence mappings: " + what + "</title><style>",
           CSS,
           "</style></head><body><div class='viz-root'>",
           f"<h1>Evidence mappings: sections × {what} ({kind})</h1><div class='legend'>Run: {esc(run)}<br>"]
    for name, text in legend(LEGEND, run):
        sw = {"Orange-outlined cell": "<span class='swatch' style='outline:2px solid var(--outline)'></span>",
              "Cell color": "<span class='swatch' style='background:var(--zero)'></span>0 " + "".join(
                  f"<span class='swatch' style='background:{c}'></span>" for c in ramp[::3]) + " 1, "
                  "<span class='swatch' style='background:var(--no-eligible)'></span>gray "}.get(name, "")
        out.append(f"<b>{esc(name)}:</b> {sw}{esc(text)}<br>")
    cols = collapse_columns(d, msgs)
    out.append(f"{len(msgs)} messages in {len(cols)} columns.")
    out.append("</div><div class='wrap'><table><thead><tr><th class='top'></th><th class='row'></th>")
    # Columns: the messages (collapsed: collapse_columns), by chain group, with a line before each group's first column
    group_start = {i for i, c in enumerate(cols) if i and d["group_of"][c[0]] != d["group_of"][cols[i - 1][0]]}

    def head(c):
        if len(c) == 1:
            return c[0][1], f"{c[0][0]}: {c[0][1]} (chain group {d['group_of'][c[0]]})"
        return (f"{c[0][1]} (+{len(c) - 1})", f"{c[0][0]}, {len(c)} messages with identical evidence mappings "
                f"(chain group {d['group_of'][c[0]]}):\n" + "\n".join(k[1] for k in c))
    out += [f"<th class='col{' g' if i in group_start else ''}' title='{esc(head(c)[1])}'><div>{esc(head(c)[0])}</div>"
            f"</th>" for i, c in enumerate(cols)]
    out.append("</tr></thead><tbody>")
    # Rows: the sections, by top-level key (named in the first column), with a line above each key's first section
    prev = None
    for s in d["sections"]:
        t = top(s)
        first = t != prev
        sep = " class='sep'" if first and prev is not None else ""
        prev = t
        out.append(f"<tr{sep}>")
        if first:
            out.append(f"<th class='top' rowspan='{span[t]}'>{esc(t or '(top level)')}</th>")
        # (hover: the section's name, then all its in-scope full keys, one per line)
        title = "\n".join([s, ""] + d["section_keys"][s])
        out.append(f"<th class='row' title='{esc(title)}'>{esc(short_section(s))}</th>")
        for i, c in enumerate(cols):
            rs = [by[(k[1], k[0], s)] for k in c]
            # (the same evidence, so the same confidence; anchored if any is)
            r = next((x for x in rs if x.cell.startswith("anchored")), rs[0])
            cls = ("c a" if r.cell.startswith("anchored") else "c") + (" g" if i in group_start else "")
            conf = "-" if r.confidence is None or pd.isna(r.confidence) else f"{r.confidence:.2f}"
            m = f"{c[0][0]}: {c[0][1]}" + (f" and {len(c) - 1} more with identical evidence mappings (header: all)"
                                           if len(c) > 1 else "")
            anchors = [json.dumps(v) + (f" ({k[1]})" if len(c) > 1 else "") for k in c
                       for v in sorted(d["anchors"][k].get(s, ()))]
            data = {"m": m, "s": s, "k": r.cell, "c": conf, "e": f"{r.evidence_keys}/{r.eligible_keys}",
                    "a": ", ".join(anchors), "v": sorted(set().union(*(x.evidence for x in rs)))}
            out.append(f"<td class='{cls}' style='background:{fill(r.confidence)}' tabindex='0' "
                       f"data-t='{esc(json.dumps(data))}'></td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    out.append("<details><summary>Table view: anchored cells</summary><table>"
               "<tr><th>message</th><th>section</th><th>cell</th><th>confidence</th><th>anchor values</th></tr>")
    for r in df[df.cell.str.startswith("anchored")].itertuples():
        conf = "-" if r.confidence is None or pd.isna(r.confidence) else f"{r.confidence:.2f}"
        out.append(f"<tr><td>{esc(r.message)}</td><td>{esc(r.section)}</td><td>{esc(r.cell)}</td><td>{conf}</td>"
                   f"<td>{esc(r.anchor_values)}</td></tr>")
    out.append("</table></details><div id='tip'></div></div><script>")
    out.append("""const tip=document.getElementById('tip');
function show(e,td){const d=JSON.parse(td.dataset.t);tip.replaceChildren();
const add=(cls,text)=>{const p=document.createElement('div');if(cls)p.className=cls;p.textContent=text;tip.append(p)};
add('v',d.c==='-'?'no eligible keys':'confidence_eligible '+d.c+' ('+d.e+' eligible keys are evidence)');
add('',d.m);add('s',d.s);
if(d.a)add('s','anchored by: '+d.a);if(d.v.length){add('s','evidence:');d.v.forEach(l=>add('kv',l))}
tip.style.display='block';const r=td.getBoundingClientRect();
let x=(e&&e.clientX||r.right)+12,y=(e&&e.clientY||r.bottom)+12;
if(x+tip.offsetWidth>innerWidth)x=innerWidth-tip.offsetWidth-8;if(y+tip.offsetHeight>innerHeight)y-=tip.offsetHeight+24;
tip.style.left=x+'px';tip.style.top=y+'px'}
document.querySelectorAll('td.c').forEach(td=>{td.addEventListener('pointermove',e=>show(e,td));
td.addEventListener('focus',()=>show(null,td));td.addEventListener('pointerleave',()=>tip.style.display='none');
td.addEventListener('blur',()=>tip.style.display='none')});""")
    out.append("</script></body></html>")
    open(path, "w").write("\n".join(out))


# The received-message matrices (--received): received messages x sent messages, linked by context ID
RECEIVED_LEGEND = [
    LABELS_NOTE,
    ("Rows", "Observed receives of this page's kind: path/pattern field of the message label (route pattern for a "
             "received request, the request's path for a received response). Ordered by chain group: messages "
             "whose chains have exactly the same set of module functions. Horizontal line between chain groups."),
    ("Columns", "Observed sends, requests then responses (a heavier line between the two): path/pattern field of the "
                "message label, ordered by chain group, numbered as in the evidence matrix. Vertical line between "
                "chain groups."),
    ("Cell color", "Blue = some event of the received message and some event of the sent message have the same "
                   "context ID (contexttrack's context_id: the request's context); shade of blue = share of the "
                   "received message's context IDs that the sent message also has. White = none shared. A request "
                   "and its own response (same context ID, req.Method and req.URL.Path) don't count as sharing it: "
                   "a received request's response, and a received response's request, aren't shown."),
]


def load_received(run):
    """The received-message matrices' rows and columns: received messages per kind, and sent messages (requests,
    then responses), each ordered by chain group (by_chain_group; the columns' groups numbered over all sent messages,
    as in load)"""
    ch = chains.message_chains(run, kinds=tuple(SENT + RECEIVED))
    sent, group_of = by_chain_group(ch, sorted(k for k in ch if k[0] in SENT))
    cols = [k for kind in SENT for k in sent if k[0] == kind]
    rows = {}
    for kind in RECEIVED:
        rows[kind] = by_chain_group(ch, sorted(k for k in ch if k[0] == kind))
    ex = chains.message_exchanges(run, kinds=tuple(SENT + RECEIVED))
    return dict(cols=cols, col_group=group_of, rows=rows, events={k: len(cs) for k, cs in ch.items()},
                ids={k: {cid for cid, _ in ex.get(k, ())} for k in ch}, exchanges={k: ex.get(k, set()) for k in ch})


# A received message's counterpart kind: the sent message of the same exchange (a received request's response, a
# received response's request)
COUNTERPART = {"Request received": "Response sent", "Response received": "Request sent"}


def shared_ids(d, r, c):
    """The context IDs received message r and sent message c share, but those they share only as one exchange's
    request and response (same context ID, req.Method and req.URL.Path); and how many those were"""
    shared = d["ids"][r] & d["ids"][c]
    if COUNTERPART[r[0]] != c[0]:
        return shared, 0
    # A shared ID counts if some pair of their events with it isn't one exchange
    rs, cs = collections.defaultdict(set), collections.defaultdict(set)
    for cid, x in d["exchanges"][r]:
        rs[cid].add(x)
    for cid, x in d["exchanges"][c]:
        cs[cid].add(x)
    kept = {cid for cid in shared if any(a != b for a in rs[cid] for b in cs[cid])}
    return kept, len(shared - kept)


def write_received_html(d, run, path, kind):
    """A received-message matrix as a table: rows the received messages of the given kind, columns the sent messages;
    a cell's color is the share of the row's context IDs the column shares (white: none), with a tooltip. Returns
    the number of cells filled."""
    rows, row_group = d["rows"][kind]
    cols, col_group = d["cols"], d["col_group"]
    what = "requests" if kind == "Request received" else "responses"
    esc = html.escape
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Received " + what + " x sent messages</title>"
           "<style>", CSS,
           # (the column for the row labels: no top-level key column here)
           "th.row{left:0}td.c{border:1px solid var(--grid)}td.c.g{border-left:2px solid var(--text-primary)}"
           "td.c.k{border-left:3px solid var(--text-primary)}tr.sep td,tr.sep th{border-top:2px solid var(--text-primary)}"
           "th.kind{text-align:left;font-size:12px;color:var(--text-primary);padding:2px 4px;border-left:3px solid "
           "var(--text-primary)}",
           # (long labels lose their start, not their end: many paths share a long prefix, e.g. .../secrets/caddy.ingress--)
           "th.row,th.col div{direction:rtl}th.row span,th.col div span{direction:ltr;unicode-bidi:embed}",
           # (a light grid, so the matrix's shape shows)
           "</style></head><body><div class='viz-root'>",
           f"<h1>Received {what} ({kind}) × sent messages</h1><div class='legend'>Run: {esc(run)}<br>"]
    out += [f"<b>{esc(name)}:</b> {esc(text)}<br>" for name, text in legend(RECEIVED_LEGEND, run)]
    n = {k: sum(1 for c in cols if c[0] == k) for k in SENT}
    sw = ("<span class='swatch' style='background:var(--zero)'></span>none " +
          "".join(f"<span class='swatch' style='background:{c}'></span>" for c in RAMP[::3]) + " all")
    out.append(f"<b>Key:</b> {sw}<br>{len(rows)} rows; {n['Request sent']} sent requests and {n['Response sent']} sent "
               f"responses.</div>")
    filled = 0
    # Header: a row naming each kind's columns, then the messages, by chain group, with a line before each group's
    # first message (and a heavier one before the responses)
    kind_start = {next(c for c in cols if c[0] == k) for k in SENT if n[k]}
    group_start = {c for i, c in enumerate(cols) if i == 0 or col_group[c] != col_group[cols[i - 1]]}
    out.append("<div class='wrap'><table><thead><tr><th class='row'></th>")
    out += [f"<th class='kind' colspan='{n[k]}'>{esc(k)}</th>" for k in SENT if n[k]]
    out.append("</tr><tr><th class='row'></th>")
    out += [f"<th class='col' title='{esc(c[0])}: {esc(c[1])} (chain group {col_group[c]}; {d['events'][c]} events)'>"
            f"<div><span>{esc(c[1])}</span></div></th>" for c in cols]
    out.append("</tr></thead><tbody>")
    for i, r in enumerate(rows):
        sep = " class='sep'" if i and row_group[r] != row_group[rows[i - 1]] else ""
        out.append(f"<tr{sep}><th class='row' title='{esc(r[0])}: {esc(r[1])} (chain group {row_group[r]}; "
                   f"{d['events'][r]} events)'><span>{esc(r[1])}</span></th>")
        for j, c in enumerate(cols):
            cls = "c" + (" k" if c in kind_start and j else " g" if c in group_start and j else "")
            shared, own = shared_ids(d, r, c)
            share = len(shared) / len(d["ids"][r]) if d["ids"][r] else 0
            filled += bool(shared)
            data = {"r": f"{r[0]}: {r[1]}", "c": f"{c[0]}: {c[1]}", "n": len(shared), "nr": len(d["ids"][r]),
                    "nc": len(d["ids"][c]), "o": own, "x": sorted(shared, key=lambda i: int(i.split(":")[-1])
                                                        if i.split(":")[-1].isdigit() else 0)[:5]}
            out.append(f"<td class='{cls}' style='background:{ramp_fill(share) if shared else 'var(--zero)'}' "
                       f"tabindex='0' data-t='{esc(json.dumps(data))}'></td>")
        out.append("</tr>")
    out.append("</tbody></table></div><div id='tip'></div></div><script>")
    out.append("""const tip=document.getElementById('tip');
function show(e,td){const d=JSON.parse(td.dataset.t);tip.replaceChildren();
const add=(cls,text)=>{const p=document.createElement('div');if(cls)p.className=cls;p.textContent=text;tip.append(p)};
add('v',d.n?d.n+' of the received message\'s '+d.nr+' context IDs shared (the sent message has '+d.nc+')'
:'no context ID shared');if(d.o)add('s',d.o+' more shared only as one exchange\'s request and response (not shown)');
add('','received: '+d.r);add('','sent: '+d.c);
if(d.n)add('s','e.g. '+d.x.join(', '));
tip.style.display='block';const r=td.getBoundingClientRect();
let x=(e&&e.clientX||r.right)+12,y=(e&&e.clientY||r.bottom)+12;
if(x+tip.offsetWidth>innerWidth)x=innerWidth-tip.offsetWidth-8;if(y+tip.offsetHeight>innerHeight)y-=tip.offsetHeight+24;
tip.style.left=x+'px';tip.style.top=y+'px'}
document.querySelectorAll('td.c').forEach(td=>{td.addEventListener('pointermove',e=>show(e,td));
td.addEventListener('focus',()=>show(null,td));td.addEventListener('pointerleave',()=>tip.style.display='none');
td.addEventListener('blur',()=>tip.style.display='none')});""")
    out.append("</script></body></html>")
    open(path, "w").write("\n".join(out))
    return filled


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="a paramtrack run: a root's directory, <out>/<module>/<root> (paramtrack's layout.py; a run from before 2026-11-15: its --out) (steps 1-8)")
    ap.add_argument("--out", required=True, help="output prefix: writes <prefix>.md, <prefix>-requests.html and "
                                                 "<prefix>-responses.html")
    ap.add_argument("--received", action="store_true",
                    help="instead, the received-message matrices (received messages x sent messages, cells not "
                         "filled in yet): <prefix>-received-requests.html and <prefix>-received-responses.html")
    a = ap.parse_args()
    if a.received:
        d = load_received(a.run)
        for kind, what in (("Request received", "requests"), ("Response received", "responses")):
            filled = write_received_html(d, a.run, f"{a.out}-received-{what}.html", kind)
            print(f"wrote {a.out}-received-{what}.html: {len(d['rows'][kind][0])} rows x {len(d['cols'])} columns, "
                  f"{filled} cells with a shared context ID")
        return
    d = load(a.run)
    df = cells(d)
    write_md(df, d, a.run, f"{a.out}.md")
    for kind, what in (("Request sent", "requests"), ("Response sent", "responses")):
        write_html(df, d, a.run, f"{a.out}-{what}.html", kind)
    print(f"wrote {a.out}.md, {a.out}-requests.html and {a.out}-responses.html")
    print(df.cell.value_counts().to_string())


if __name__ == "__main__":
    main()
