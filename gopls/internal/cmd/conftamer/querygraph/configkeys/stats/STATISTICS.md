See [TERMINOLOGY.md](TERMINOLOGY.md). "Key" below means "full key"
# Coverage

## Dropped keys
This table shows how many keys are dropped in each step of the workflow.

Each column counts the keys that satisfy the corresponding property in TERMINOLOGY.md, as well as the previous column's property. (One column's property is often a subset of the previous, but there are exceptions, e.g. the /concreteconfig skill can set non-target keys if needed for the config to be valid.)

- "Keys" and "Sections": The percent is of the row's value in the previous column.
- "Keys per section": Distribution of keys per section (median/mean), across sections with the property.
- "Min. confidence": Distribution of minimum confidence of the keys (median/mean)
  - For each key, its value in the distribution is the reciprocal of its section size*, as a percent.

The first three rows show how many keys drop per step, and how evenly they drop across sections.
The last row shows how the minimum confidence of keys changes across each step. The confidence of an inferred mapping for message M is "number of evidence mappings for M in the section"/"section size". Thus the minimum confidence represents the case where a mapping for M is the only evidence for M in its section.

*When considering section size, we count in-scope keys in that section only (this makes no difference for Prometheus, since every section is either entirely in-scope or entirely not).


|                  | Full   | In-scope    | Target      | Concrete    | Unambiguous   |
|:-----------------|:-------|:------------|:------------|:------------|:--------------|
| Keys             | 3106   | 536 (17%)   | 331 (62%)   | 263 (79%)   | 222 (84%)     |
| Sections         | 497    | 92 (19%)    | 85 (92%)    | 84 (99%)    | 83 (99%)      |
| Keys per section | 4/6.2  | 4/5.8       | 4/3.9       | 3/3.1       | 3/2.7         |
| Min. confidence  | -      | 14.3%/17.2% | 14.3%/17.8% | 14.3%/19.1% | 14.3%/19.2%   |

## Drop reasons
These tables show the reasons for the drops from one column to the next.

Each dropped key counts once, under the combination of reasons that apply to it.

<table>
<tr><th>In-scope -> Target</th><th style="border-right: 1px solid">Dropped</th><th>Target -> Concrete</th><th style="border-right: 1px solid">Dropped</th><th>Concrete -> Unambiguous</th><th style="border-right: 1px solid">Dropped</th></tr>
<tr><td>Unsearchable kind</td><td style="border-right: 1px solid">134/205 (65%)</td><td>Secret-manager reference (*_ref): fails at runtime</td><td style="border-right: 1px solid">63/68 (93%)</td><td>A value shared with another concrete key + a value that's a string literal in the code</td><td style="border-right: 1px solid">26/41 (63%)</td></tr>
<tr><td>Stored transformed</td><td style="border-right: 1px solid">71/205 (35%)</td><td>Would contact endpoints outside the testbed</td><td style="border-right: 1px solid">5/68 (7%)</td><td>A value that's a string literal in the code</td><td style="border-right: 1px solid">13/41 (32%)</td></tr>
<tr><td></td><td style="border-right: 1px solid"></td><td></td><td style="border-right: 1px solid"></td><td>A value shared with another concrete key</td><td style="border-right: 1px solid">2/41 (5%)</td></tr>
</table>



# TODO
- Clarify in skill prompt how to parse/prioritize output of score.py
  - But allow run to add comments like in the most recent run (summarizes why keys were unset)
- Ignore ambiguous values in evidence mappings for now
  - Dups: eventually - divide confidence of evidence mapping by # of dups
  - String lit, unclear how much to lower confidence => ignore these (i.e. don't search, or ignore their mappings)
- From STATISTICS_OLD_PRUNED.md: pngs, graphs, documented keys


# Mechanics
- **Code:** [stats.py](stats.py)
- Numbers above are from Prometheus/prombench on 2026-11-09 (`/tmp/paramtrack-full-2026-11-09`, concrete config from the 2026-11-08 skill run), with `--ignore` as committed then (paramtrack/ignore/prometheus.txt) and `--transformed` as in MODULE.md's list then: `*.regex`, `*.proxy_url`, `*.api_server`, `*.tls_config.{ca,cert,key}`, `*.no_proxy` (predated the transformed file)

# Details of counting keys
- Concrete keys are translated to their full key before counting, which matters for:
  - Maps: `global.external_labels.cluster` and `global.external_labels.monitor` both count as `global.external_labels.*`
  - Lists: every scrape job name counts as `scrape_configs.job_name`
- Two patterns are dropped from full keys, for Prometheus: `scrape_configs.*.*`, `alerting.alertmanagers.*.*`
  - (They're placeholders for anything discovery.Configs' dynamic unmarshaler might accept; the real SD keys are already counted through the previous exception)
  - These don't count toward section sizes.

# Example
For a small example concrete config showing the various drop reasons, along with the section keys, see [example](key-categories-example.yaml).
