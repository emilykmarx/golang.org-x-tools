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

Usage: uv run --with pandas --with tabulate python evidence_matrix.py --run <paramtrack out dir> --out <prefix>
writes <prefix>.md, and a page per message kind: <prefix>-requests.html (Request sent) and <prefix>-responses.html
(Response sent)
"""

import argparse
import collections
import csv
import html
import json
import os
import sys

import pandas as pd

import chains  # (next to this file)
import matches  # (next to this file)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import stats  # noqa: E402  (configkeys/stats/stats.py: the concrete keys' values and full keys)

SENT = ["Request sent", "Response sent"]

LEGEND = [
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


def load(run):
    """Everything the matrix needs, from the run directory"""
    fkdf = pd.read_csv(f"{run}/8-stats/full_keys.csv", keep_default_na=False, dtype=str)
    section_of = dict(zip(fkdf.key, fkdf.section))  # in-scope full key => section
    section_keys = fkdf.groupby("section").key.apply(lambda ks: sorted(set(ks))).to_dict()  # section => its keys
    sections = sorted(set(fkdf.section), key=lambda s: (s.split(".")[0].split(" ")[0], s))

    # Unambiguous keys (matches.unambiguous_keys: stats.py's rule), with the concrete keys' values
    unamb_keys, covered_by, config, _ = matches.unambiguous_keys(run)
    searched = set(open(f"{run}/4-configkeys/param_values.txt").read().splitlines())

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
    for r in csv.DictReader(open(f"{run}/6-label-values/joined.csv", newline="")):
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

    # Anchors: (kind, message) => section => the values that anchor it
    anchors = collections.defaultdict(lambda: collections.defaultdict(set))
    for k in msgs:
        for v, secs in value_sections.items():
            if v in k[1] or k[1] in v:
                for s in secs:
                    anchors[k][s].add(v)
    return dict(sections=sections, section_keys=section_keys, eligible=eligible, evidence=evidence, via=via, msgs=order, group_of=group_of,
                anchors=anchors)


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
    L += [f"- **{name}:** {text}" for name, text in LEGEND] + [""]
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


def write_html(df, d, run, path, kind):
    """The matrix as a table: each cell's color is its confidence (blue ramp, palette.md's sequential steps), gray if
    the section has no eligible keys, with an outline if anchored; a tooltip per cell. Light and dark modes. Only the
    messages of the given kind (Request sent or Response sent)."""
    df = df[df.kind_msg == kind]
    msgs = [k for k in d["msgs"] if k[0] == kind]
    what = "requests" if kind == "Request sent" else "responses"
    ramp = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf",
            "#1c5cab", "#184f95", "#104281", "#0d366b"]  # steps 100..700

    def fill(c):
        if c is None or pd.isna(c):
            return "var(--no-eligible)"
        if c == 0:
            return "var(--zero)"
        return ramp[min(len(ramp) - 1, int(c * (len(ramp) - 1) + 0.5))]

    def top(s):
        return s.split(".")[0].split(" ")[0]

    span = collections.Counter(top(s) for s in d["sections"])
    by = {(r.message, r.kind_msg, r.section): r for r in df.itertuples()}
    esc = html.escape
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>Evidence mappings: " + what + "</title><style>",
           """.viz-root{color-scheme:light;--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;
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
details{margin-top:12px;color:var(--text-secondary)}""",
           "</style></head><body><div class='viz-root'>",
           f"<h1>Evidence mappings: sections × {what} ({kind})</h1><div class='legend'>Run: {esc(run)}<br>"]
    for name, text in LEGEND:
        sw = {"Orange-outlined cell": "<span class='swatch' style='outline:2px solid var(--outline)'></span>",
              "Cell color": "<span class='swatch' style='background:var(--zero)'></span>0 " + "".join(
                  f"<span class='swatch' style='background:{c}'></span>" for c in ramp[::3]) + " 1, "
                  "<span class='swatch' style='background:var(--no-eligible)'></span>gray "}.get(name, "")
        out.append(f"<b>{esc(name)}:</b> {sw}{esc(text)}<br>")
    out.append("</div><div class='wrap'><table><thead><tr><th class='top'></th><th class='row'></th>")
    # Columns: the messages, by chain group, with a line before each group's first message
    group_start = {k for i, k in enumerate(msgs) if i == 0 or d["group_of"][k] != d["group_of"][msgs[i - 1]]}
    out += [f"<th class='col{' g' if k in group_start and i else ''}' title='{esc(k[0])}: {esc(k[1])} "
            f"(chain group {d['group_of'][k]})'><div>{esc(k[1])}</div></th>" for i, k in enumerate(msgs)]
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
        for i, k in enumerate(msgs):
            r = by[(k[1], k[0], s)]
            cls = ("c a" if r.cell.startswith("anchored") else "c") + (" g" if k in group_start and i else "")
            conf = "-" if r.confidence is None or pd.isna(r.confidence) else f"{r.confidence:.2f}"
            data = {"m": f"{k[0]}: {k[1]}", "s": s, "k": r.cell, "c": conf, "e": f"{r.evidence_keys}/{r.eligible_keys}",
                    "a": ", ".join(json.dumps(v) for v in sorted(d["anchors"][k].get(s, ()))), "v": r.evidence}
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="paramtrack --out directory (steps 1-8)")
    ap.add_argument("--out", required=True, help="output prefix: writes <prefix>.md, <prefix>-requests.html and "
                                                 "<prefix>-responses.html")
    a = ap.parse_args()
    d = load(a.run)
    df = cells(d)
    write_md(df, d, a.run, f"{a.out}.md")
    for kind, what in (("Request sent", "requests"), ("Response sent", "responses")):
        write_html(df, d, a.run, f"{a.out}-{what}.html", kind)
    print(f"wrote {a.out}.md, {a.out}-requests.html and {a.out}-responses.html")
    print(df.cell.value_counts().to_string())


if __name__ == "__main__":
    main()
