#!/bin/bash
# Runs the concreteconfig skill unattended (SKILL.md, "Running headless"): a detached `claude -p` with the given
# prompt and an allowlist of the tools the skill needs, so it survives the calling session closing. Writes, in the
# output dir: agent.jsonl (the transcript, stream-json), agent.err, and agent.exit ("exit N") when it's done.
# Usage: run_headless.sh <prompt file> <output dir> [time limit in seconds, default 14400]
set -euo pipefail
prompt=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
mkdir -p "$2"
out=$(cd "$2" && pwd)
limit=${3:-14400}
cd /home/emily/projects/config_tracing/golang.org-x-tools
allow=(Read Write Edit Glob Grep Skill
  'Bash(gcloud:*)' 'Bash(go:*)' 'Bash(uv:*)' 'Bash(python3:*)' 'Bash(timeout:*)' 'Bash(cd:*)' 'Bash(set:*)'
  'Bash(ls:*)' 'Bash(cat:*)' 'Bash(head:*)' 'Bash(tail:*)' 'Bash(grep:*)' 'Bash(diff:*)' 'Bash(wc:*)'
  'Bash(mkdir:*)' 'Bash(cp:*)' 'Bash(mv:*)' 'Bash(rm:*)' 'Bash(chmod:*)' 'Bash(md5sum:*)' 'Bash(sort:*)'
  'Bash(uniq:*)' 'Bash(echo:*)' 'Bash(printf:*)' 'Bash(date:*)' 'Bash(find:*)' 'Bash(sed:*)' 'Bash(awk:*)'
  'Bash(tee:*)' 'Bash(sleep:*)' 'Bash(cut:*)' 'Bash(tr:*)' 'Bash(openssl:*)' 'Bash(base64:*)' 'Bash(jq:*)'
  'Bash(test:*)' 'Bash(ps:*)')
setsid nohup bash -c '
  timeout "$1" claude -p "$(cat "$2")" --output-format stream-json --verbose --allowedTools "${@:5}" \
    --add-dir /home/emily/projects/config_tracing --add-dir /tmp --add-dir "$HOME/go/pkg/mod" \
    > "$3/agent.jsonl" 2> "$3/agent.err"
  echo "exit $?" > "$3/agent.exit"' _ "$limit" "$prompt" "$out" unused "${allow[@]}" > /dev/null 2>&1 < /dev/null &
echo "started (pid $!); transcript: $out/agent.jsonl; done when $out/agent.exit exists"
