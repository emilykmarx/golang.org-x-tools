"""Channel-identity investigation (2026-11-16), throwaway: an upper bound for associating received and sent messages by
channel identity. Every channel parameter counts (by element type, chan struct{} and chan error included), with
evidence_matrix's directed rule. Identity can only remove cells from this. Prints the cell count, and per
(send function, recv function, element) the number of (received, sent) message pairs, with examples.

  cd gopls/internal/cmd/conftamer/querygraph
  uv run --with pandas --with pyyaml --with matplotlib --with numpy python configkeys/stats/messages/chanid/upper.py \
      <run>/<module>/<root> <matches.chans.csv>

Runs used: /tmp/paramtrack-k8s-fixtures-2026-11-15/kube-apiserver/audit_policy with
gopls/results/k8s_apiserver/matrices-2026-11-16/chans-kube-apiserver.csv (untracked); /tmp/paramtrack-combined-2026-11-15/caddy/caddy
with caddy.chans.csv here (Caddy's chans output, built and run briefly on the VM)."""
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chains  # noqa: E402
import evidence_matrix as em  # noqa: E402


def chan_types_all(path):
    """chains.chan_types, keyed by every parameter's element type instead of its named types"""
    out = collections.defaultdict(lambda: collections.defaultdict(set))
    for row in csv.DictReader(open(path)):
        out[row["function"].replace("[...]", "")]["chan " + row["element"]].add(row["direction"])
    return out


chains.chan_types = chan_types_all


def main(run, chans):
    d = em.load_received(run, chans)
    pairs, recvs, sents, cells = collections.Counter(), collections.defaultdict(set), collections.defaultdict(set), 0
    for kind, (rows, _) in d["rows"].items():
        for r in rows:
            for c in d["cols"]:
                ts = em.shared_chan_types(d, r, c)
                cells += bool(ts)
                for t in ts:
                    for fr, dr in d["chan_types"][r][t]:
                        if dr not in ("send", "both"):
                            continue
                        for fc, dc in d["chan_types"][c][t]:
                            if dc in ("recv", "both"):
                                pairs[(fr, fc, t)] += 1
                                recvs[(fr, fc, t)].add(r)
                                sents[(fr, fc, t)].add(c)
    print("cells", cells)
    for (fr, fc, t), n in pairs.most_common():
        print(n, t, "|", chains.short(fr), "->", chains.short(fc), "| e.g.", sorted(recvs[(fr, fc, t)])[:2],
              sorted(sents[(fr, fc, t)])[:2])


if __name__ == "__main__":
    main(*sys.argv[1:3])
