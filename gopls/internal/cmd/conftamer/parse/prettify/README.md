# prettify

Adds attributes useful for visualizing to a graph serialized by gopls (`.text`), and writes it as a `.gv`. Use it to try out visualizations without rerunning gopls.
With `--split-roots`, it also splits the graph into smaller subgraphs that are easier to view.

## Usage
```
go run . --infile <graph serialized by Serialize, e.g. unmarshaler_subgraph.text> \
  --outfile <output .gv, e.g. unmarshaler_subgraph_pretty.gv> \
  [--module-prefix <module as in go.mod, e.g. github.com/prometheus/prometheus>] \
  [--split-roots] [--misc-max-nodes <n, default 10>]
```
Requires `dot` (graphviz), and for `--split-roots`, the Python packages `igraph` and `pydot`.

## Output
Below, `<stem>` is `--outfile` without its extension.
- `<stem>.gv.dot`: the whole graph, with a hierarchical layout from `dot -Tdot`. Each node gets these attributes:
  - `pkg`: the node's package
  - `label`: a short label
  - `type`: the full type name. This is a copy of the node ID, because gephi only shows the ID in Data Lab, not Overview.

Every `.gv` that gets a `dot` layout is deleted afterwards, leaving only the `.gv.dot`.

With `--split-roots`, `split_roots.py` splits the whole graph into weakly connected components:
- `<stem>_misc_<n>nodes.gv`: all components with at most `--misc-max-nodes` nodes, combined into one graph. `<n>` is the number of nodes in the file.
- `<stem>_roots/comp<i>/<n>nodes_<root>.gv.dot`: for each larger component, one file per root, holding everything reachable from that root. `<n>` is the number of nodes in the file.
  - Components are numbered from largest to smallest, so file names are stable across runs. Use `ls -v` to sort.
  - Subgraphs from roots in the same component can share nodes.
  - Each file has a `dot` layout.
- `<stem>_roots/comp<i>/misc_<n>nodes.gv`: the per-root subgraphs in component `<i>` with at most `--misc-max-nodes` nodes, combined into one graph. Their roots don't get their own files.
- `<stem>_cycles_<n>nodes.gv`: nodes in the larger components that aren't reachable from any root, combined into one graph. Only written if there are any.
  - A root is a node with no incoming edges, so these are nodes only reachable from a cycle with no edges coming into it (e.g. recursive types).
  - A component made entirely of such a cycle gets an empty `comp<i>/` folder.
- `<stem>_roots/` is deleted and rewritten on each run, and so are old `<stem>_misc*.gv` and `<stem>_cycles*.gv` files.
- Roots of components in the misc graph don't get their own files.

Components and roots are printed with their node and edge counts.

### Which files overlap
- `<stem>_misc_<n>nodes.gv` shares no nodes with any other output file, because it holds only whole components.
- Different `comp<i>/` folders share no nodes, because each holds one weakly connected component.
- Within a `comp<i>/` folder, the per-root files and `misc_<n>nodes.gv` can share nodes, because two roots can reach the same node.
- `<stem>_cycles_<n>nodes.gv` shares no nodes with any other file, because its nodes aren't reachable from any root.

### Laying out the misc and cycles graphs
No layout is applied to `<stem>_misc_<n>nodes.gv`, the per-component `misc_<n>nodes.gv` files or `<stem>_cycles_<n>nodes.gv`, so they're the only `.gv` files left.
- Each misc graph holds many small, disconnected subgraphs, and `dot`'s hierarchical layout doesn't work well on that. Gephi's `Force Atlas` or `Force Atlas 2` will likely be fine, since each subgraph is small.
- The cycles graph has no roots, so a hierarchical layout has no natural top.
