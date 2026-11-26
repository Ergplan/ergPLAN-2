#!/usr/bin/env bash
# Types one OpenAI key into the server's .env as both OPENAI_API_KEY (the app and the reviewer) and
# BILLX_OPENAI_API_KEY (billx), with hidden typing. The key is never echoed, never put on a command
# line (it reaches the editing step through its standard input), and .env stays mode 600.
#   tools/dev/set-openai-key.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV="$ROOT/.env"
[ -f "$ENV" ] || { echo "no $ENV"; exit 1; }
read -rsp "OpenAI key (typing is hidden): " KEY
echo
[ -n "$KEY" ] || { echo "nothing typed; .env unchanged"; exit 1; }
case "$KEY" in *[[:space:]]*) echo "the key has a space or a line break in it; .env unchanged"; exit 1 ;; esac
printf '%s' "$KEY" | python3 -c '
import os, sys
path = sys.argv[1]
key = sys.stdin.read()
names = ("OPENAI_API_KEY", "BILLX_OPENAI_API_KEY")
lines = open(path).read().splitlines()
seen = set()
for i, line in enumerate(lines):
    name = line.split("=", 1)[0].strip()
    if name in names:
        lines[i] = f"{name}={key}"
        seen.add(name)
lines += [f"{n}={key}" for n in names if n not in seen]
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, path)
' "$ENV"
unset KEY
chmod 600 "$ENV"
echo "OPENAI_API_KEY and BILLX_OPENAI_API_KEY are set in .env (not shown). Tell Claude: done"
