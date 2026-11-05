#!/usr/bin/env bash
# Draw the Unmarshaler Subgraph with each node sized by its number of full keys and coloured by the fraction of them
# whose values argmatch matched (see STATISTICS.md, "Visualizing it"). Run stats.py first: it writes the node
# attributes (node_coverage.csv, node_coverage_keyed.csv), node_legend.csv and sd_configs.csv. Writes, in RESULTS:
# - <stem> = unmarshaler_subgraph_coverage: the whole graph, counting every full key (node_coverage.csv)
# - <stem> = unmarshaler_subgraph_coverage_keyed: without the SD configs sd_configs.csv leaves out, or any full key
#   under them, and with the nodes left with no full keys contracted away (node_coverage_keyed.csv; see prettify's
#   --keep-nodes-with and --exclude-nodes)
# For each:
#   <stem>.gv.dot        the graph with the node attributes and a legend (graph label),
#                        laid out left to right by dot (open in gephi)
#   <stem>_preview.png   a graphviz rendering of it, at the same node positions
# Usage: visualize.sh [RESULTS]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
R=${1:-$here/../../../../../../results/prom/stats}
R=$(cd "$R" && pwd)

# Adds the legend and graph-wide attributes:
# add_legend <in .gv.dot> <out .gv> <title> <description lines, separated by newlines> [<colour for 0% matched nodes>]
add_legend() {
  python3 -I - "$1" "$R/node_legend.csv" "$2" "$3" "$4" "${5:-}" <<'PY'
import csv, html, re, sys
graph_path, legend_path, out_path, title, description, zero_color = sys.argv[1:7]
ZERO = "0% matched"
s = open(graph_path).read()
if zero_color:
    # Recolour the 0% matched nodes (node blocks as dot -Tdot writes them)
    def recolor(m):
        block = m.group(0)
        if re.search(r'matched_bin="?' + re.escape(ZERO), block):
            block = re.sub(r'\b(fill)?color="[^"]*"', lambda c: f'{c.group(1) or ""}color="{zero_color}"', block)
        return block
    s = re.sub(r'^\t"[^"]+"\t\[.*?\];', recolor, s, flags=re.S | re.M)
# Put each node's label (prettify's short label) outside its circle, where it fits
def outside(m):
    block = m.group(0)
    lab = re.search(r'\blabel=("(?:[^"\\]|\\.)*"|[^,\]\s]+)', block)
    if lab:
        block = block.replace(lab.group(0), f'label="", xlabel={lab.group(1)}', 1)
    return block
s = re.sub(r'^\t"[^"]+"\t\[.*?\];', outside, s, flags=re.S | re.M)
# Nodes' matched_bin attributes, as dot -Tdot writes them
nodes = re.findall(r'^\t"[^"]+"\t\[(.*?)\];', s, re.S | re.M)
bins = [m.group(1).strip('"') for m in (re.search(r'matched_bin=("[^"]*"|[^,\]\s]+)', n) for n in nodes) if m]
rows = list(csv.DictReader(open(legend_path)))
for r in rows:
    if zero_color and r["matched_bin"] == ZERO:
        r["color"] = zero_color
# Only colours that some node in this graph has
legend = "".join(f'<TR><TD BGCOLOR="{r["color"]}" WIDTH="18"> </TD><TD ALIGN="LEFT">{r["matched_bin"]} '
                 f'({bins.count(r["matched_bin"])} nodes)</TD></TR>' for r in rows if bins.count(r["matched_bin"]))
text = "".join(f'<TR><TD COLSPAN="2" ALIGN="LEFT">{html.escape(line)}</TD></TR>' for line in description.split("\n"))
label = (f'<<TABLE BORDER="0" CELLSPACING="2"><TR><TD COLSPAN="2" ALIGN="LEFT"><B>{html.escape(title)}</B></TD></TR>'
         f'{text}{legend}</TABLE>>')
s = re.sub(r"(digraph[^{]*\{)", lambda m: m.group(1) + f'\n\tlabel={label};\n\tlabelloc=t;\n\tlabeljust=l;\n\trankdir=LR;'
           '\n\tforcelabels=true;\n\tfontname="Helvetica";\n\tnode [fontname="Helvetica", fontsize=10];'
           '\n\tedge [color="#9a9890"];', s, count=1)
open(out_path, "w").write(s)
PY
}

# render <stem> <node attributes csv> <title> <description> <colour for 0% matched nodes, or ""> [extra attrs.go flags...]
render() {
  local stem=$1 attrs=$2 title=$3 description=$4 zero_color=$5
  shift 5
  (cd "$here/../../../parse/prettify" && timeout 300 go run . --infile "$R/unmarshaler_subgraph.text" \
    --outfile "$R/$stem.gv" --module-prefix github.com/prometheus/prometheus \
    --node-attrs "$attrs" "$@")

  local with_legend
  with_legend=$(mktemp --suffix=.gv)
  add_legend "$R/$stem.gv.dot" "$with_legend" "$title" "$description" "$zero_color"
  # Lay out left to right, then render at the positions dot wrote (neato -n2 uses the pos attributes rather than
  # laying out again)
  timeout 120 dot -Tdot "$with_legend" -o "$R/$stem.gv.dot"
  rm "$with_legend"
  timeout 120 neato -n2 -Tpng -Gdpi=72 "$R/$stem.gv.dot" -o "$R/${stem}_preview.png"
  echo "wrote $R/$stem.gv.dot and $R/${stem}_preview.png"
}

# A node's full keys are those whose Unmarshaler it is (the type holding the value)
render unmarshaler_subgraph_coverage "$R/node_coverage.csv" \
  "Unmarshaler Subgraph: each node's full keys, and how many of them argmatch matched" \
  "Area: the number of the node's full keys (keys whose Unmarshaler is the node), counting the keys under every SD config.
E.g. TLSConfig counts scrape_configs.tls_config.ca_file, and also scrape_configs.azure_sd_configs.tls_config.ca_file and its copies in every other SD config.
Colour: the fraction of those keys whose values argmatch matched. Grey: no full keys." ""

# The SD configs left out (sd_configs.csv): as a regex for --exclude-nodes, and the kept ones' keys for the description
read -r unmatched_sd kept_sd n_left_out < <(python3 -I - "$R/sd_configs.csv" <<'PY'
import csv, re, sys
rows = list(csv.DictReader(open(sys.argv[1])))
out = [r for r in rows if r["left_out"] == "True"]
kept = sorted({r["sd_key"] for r in rows if r["left_out"] != "True"})
print("^(" + "|".join(re.escape(r["node"]) for r in out) + ")$", ",".join(kept), len({r["sd_key"] for r in out}))
PY
)
# Every node here has full keys, so grey (in the whole graph: no full keys) is free for 0% matched.
render unmarshaler_subgraph_coverage_keyed "$R/node_coverage_keyed.csv" \
  "Unmarshaler Subgraph without the unmatched SD configs: each node's remaining full keys, and how many of them argmatch matched" \
  "Left out: the ${n_left_out} SD configs none of whose full keys argmatch matched, and every full key under them, wherever it ends.
E.g. scrape_configs.azure_sd_configs.tls_config.ca_file no longer counts for TLSConfig. Kept SD configs: ${kept_sd//,/, }.
Nodes left with no full keys are contracted away (their edges joined up).
Area: the number of the node's remaining full keys (keys whose Unmarshaler is the node). Colour: the fraction of those whose values argmatch matched." \
  "#c9c8c2" --keep-nodes-with full_keys --exclude-nodes "$unmatched_sd"
