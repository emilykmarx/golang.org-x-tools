"""Channel-identity investigation (2026-11-16), throwaway: kube-apiserver's cells under approximate channel identity,
from reading the source instead of recording channel addresses. Reflector.watch's resyncerrc is made per
watchWithResync call, so a pair survives only within one reflector (approximated by the same resource path). The
Repair loops' stopCh comes from their own async.Runner, so only Repair -> Repair pairs survive. Prints how many
survive, and whether they already share a context ID. Same usage as upper.py (kube-apiserver run only)."""
import collections
import sys

import upper
from upper import chains, em

REFL = "k8s.io/client-go/tools/cache.(*Reflector).watch"
REP = {"pkg/registry/core/service/ipallocator/controller.(*Repair).RunUntil",
       "pkg/registry/core/service/portallocator/controller.(*Repair).RunUntil"}


def main(run, chans):
    d = em.load_received(run, chans)

    def fns(k, t, dirs):
        return {chains.short(f) for f, dr in d["chan_types"][k].get(t, ()) if dr in dirs}

    stat, ex = collections.Counter(), collections.defaultdict(list)
    for kind, (rows, _) in d["rows"].items():
        for r in rows:
            for c in d["cols"]:
                for t in em.shared_chan_types(d, r, c):
                    fr, fc = fns(r, t, ("send", "both")), fns(c, t, ("recv", "both"))
                    if t == "chan error" and REFL in fr and REFL in fc:
                        fam, same = "reflector", r[1].split("?")[0] == c[1].split("?")[0]
                    elif t == "chan struct{}" and fr & REP and fc & REP:
                        fam, same = "repair", True
                    else:
                        continue
                    if not same:
                        stat[(fam, "type-only")] += 1
                        continue
                    ids, _ = em.shared_ids(d, r, c)
                    key = (fam, "identity", "shares ctx id" if ids else "NEW")
                    stat[key] += 1
                    ex[key].append((r, c))
    for k, n in sorted(stat.items()):
        print(n, k)
    for k, v in ex.items():
        print(k, v[:6])


if __name__ == "__main__":
    main(*sys.argv[1:3])
