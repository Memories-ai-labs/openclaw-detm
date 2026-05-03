#!/bin/bash
# Usage: extract_run.sh <run_id> <session_jsonl_path> <task_id> <start_iso> <end_iso>
set -e
RUN_ID="$1"
SESS_PATH="$2"
TASK_ID="$3"
START_TS="$4"
END_TS="$5"
RESULTS=/home/alex/openclaw-memoriesai/experiments/holo3-vs-flagships-2026-04-25
OUT="$RESULTS/$RUN_ID"
mkdir -p "$OUT"

# 1. Conversation JSONL: filter the session file to messages within the time window
python3 <<PY > "$OUT/conversation.jsonl"
import json
src = "$SESS_PATH"
start, end = "$START_TS", "$END_TS"
with open(src) as f:
    for line in f:
        try:
            d = json.loads(line)
            if d.get('type') == 'session':
                print(line.strip()); continue
            ts = d.get('timestamp','')
            if start <= ts <= end:
                print(line.strip())
        except: pass
PY
echo "  conversation.jsonl: $(wc -l < $OUT/conversation.jsonl) lines"

# 2. Tool histogram from main-LLM tool calls
python3 <<PY > "$OUT/tool-histogram.txt"
import json
from collections import Counter
c = Counter()
for line in open("$OUT/conversation.jsonl"):
    try:
        d = json.loads(line)
        if d.get('type') != 'message': continue
        m = d['message']
        if m.get('role') != 'assistant': continue
        for blk in m.get('content', []):
            if blk.get('type') == 'toolCall':
                c[blk.get('name','?')] += 1
    except: pass
print("Run $RUN_ID")
print("Window: $START_TS -> $END_TS")
print()
print("Main-LLM tool-call histogram:")
for k, v in c.most_common():
    print(f"  {v:3d}  {k}")
print(f"\n  TOTAL: {sum(c.values())}")
PY
cat "$OUT/tool-histogram.txt"

# 3. Daemon log slice: grep by start time prefix
START_HM=$(echo "$START_TS" | cut -c12-16)
END_HM=$(echo "$END_TS" | cut -c12-16)
awk -v s="$START_HM" -v e="$END_HM" '
match($0, /^[0-9]{2}:[0-9]{2}/) {
    t = substr($0, 1, 5)
    if (t >= s && t <= e) print
}' ~/.agentic-computer-use/logs/debug.log > "$OUT/daemon.log"
echo "  daemon.log: $(wc -l < $OUT/daemon.log) lines"

# 4. Wall-clock from the user prompt to assistant final
python3 <<PY > "$OUT/wallclock.txt"
import json
from datetime import datetime
first_user = None
last_asst = None
for line in open("$OUT/conversation.jsonl"):
    try:
        d = json.loads(line)
        if d.get('type') != 'message': continue
        m = d['message']
        ts = d.get('timestamp','')
        if m.get('role') == 'user' and first_user is None:
            for blk in m.get('content', []):
                if blk.get('type') == 'text' and 'NEW TASK' in blk.get('text',''):
                    first_user = ts
                elif blk.get('type') == 'text' and 'Connect with 5' in blk.get('text',''):
                    first_user = ts
        if m.get('role') == 'assistant':
            for blk in m.get('content', []):
                if blk.get('type') == 'text':
                    last_asst = ts
if first_user and last_asst:
    a = datetime.fromisoformat(first_user.replace('Z','+00:00'))
    b = datetime.fromisoformat(last_asst.replace('Z','+00:00'))
    print(f"first_user={first_user}")
    print(f"last_assistant={last_asst}")
    print(f"wall_clock_s={(b-a).total_seconds():.1f}")
else:
    print(f"first_user={first_user}")
    print(f"last_assistant={last_asst}")
PY
cat "$OUT/wallclock.txt"

# 5. Post-run screenshot
DISPLAY=:99 scrot -o /tmp/run_${RUN_ID}.png
python3 -c "
from PIL import Image
im = Image.open('/tmp/run_${RUN_ID}.png')
im.thumbnail((1440, 900))
im.save('$OUT/screenshot.jpg', 'JPEG', quality=72)
"
ls -la "$OUT/screenshot.jpg"

echo
echo "=== $RUN_ID artifacts saved to $OUT ==="
