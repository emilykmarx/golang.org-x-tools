import argparse
import json
from collections.abc import Iterable
from typing import Literal

import igraph as ig


def influence_subgraph(
  graph: ig.Graph,
  vertices: Iterable[str],
  *,
  direction: Literal["ancestors", "descendants", "both"],
) -> ig.Graph:
  """Return the subgraph containing the vertices"""

  if direction not in {"ancestors", "descendants", "both"}:
    raise ValueError(f"unknown influence direction {direction!r}")

  subgraph_vertices = set()
  modes = {
    "ancestors": ("in",),
    "descendants": ("out",),
    "both": ("in", "out"),
  }
  for vertex in vertices:
    for mode in modes[direction]:
      subgraph_vertices.update(graph.subcomponent(vertex, mode=mode))

  return graph.induced_subgraph(subgraph_vertices)

def getHash(type, loaded_list):
  return loaded_list["List"][type]

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("-graph", help="Graph file (ok: graphml/gml, not ok: gv)", type=str, required=True)
  parser.add_argument("-list", help="List file mapping types to node hashes", type=str, required=True)
  parser.add_argument("-start_type", help="Query start type", type=str, required=True)
  parser.add_argument("-outfile", help="Outfile for graphml subgraph", type=str, required=True)
  args = parser.parse_args()

  with open(args.list) as f:
    loaded_list = json.load(f)
    start_hash = getHash(args.start_type, loaded_list)

  print("Querying for subgraph containing", args.start_type)
  g: ig.Graph = ig.Graph.Read(args.graph)

  subgraph = influence_subgraph(g, [start_hash], direction="both")
  subgraph.write_graphml(args.outfile)

if __name__ == "__main__":
  main()
