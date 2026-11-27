#!/bin/sh
# Deploy PixerTool on the Raspberry Pi: when origin/main has a new commit and both CI test jobs
# passed on exactly that commit, fast-forward this checkout to it and run the tests; if they fail
# here, go back to the previous commit. Cron runs it every 10 minutes at :05, :15 ... so it never
# starts alongside the card jobs; it prints nothing when there is nothing to do:
#   5-59/10 * * * * flock -n -o /tmp/pixer-deploy.lock $HOME/chyi/github/PixerTool/misc/pixer-deploy.sh
# (-o keeps the lock out of the pixer_web.py this may restart, which would otherwise hold it for good.)
# A deploy may update this script while it runs; git writes a new file rather than rewriting this
# one in place, so the running copy is not disturbed.
# GitHub is reached through the Mac mini's proxy, like the card jobs; both git and Python honour it.
export HTTPS_PROXY=http://10.1.7.106:1082
REPO=$(cd "$(dirname "$0")/.." && pwd)
LOG="$HOME/pixer-deploy.log"
SEEN="$HOME/.pixer-deploy-seen"   # the last verdict logged, so each commit is reported once
CHECKS="test (3.9)
test (3.13)"

cd "$REPO" || exit 1
git fetch -q origin main || exit 1
sha=$(git rev-parse FETCH_HEAD)
prev=$(git rev-parse HEAD)
[ "$sha" = "$prev" ] && exit 0

report() {  # log "$1" for this commit unless it was the last thing logged
  [ "$(cat "$SEEN" 2>/dev/null)" = "$sha $1" ] && return 0
  echo "$sha $1" > "$SEEN"
  echo "$(date) $sha: $1" >> "$LOG"
}
[ "$(cat "$SEEN" 2>/dev/null)" = "$sha failed on the Pi" ] && exit 1

# success, pending or failed, from the CI check runs on this commit.
verdict=$(python3 - "$sha" "$CHECKS" <<'PY'
import json, sys, urllib.request
sha, wanted = sys.argv[1], set(sys.argv[2].splitlines())
url = f"https://api.github.com/repos/ChyiYaqing/PixerTool/commits/{sha}/check-runs?filter=latest&per_page=100"  # latest run of each check
request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "pixer-deploy"})
runs = {r["name"]: r for r in json.load(urllib.request.urlopen(request, timeout=30))["check_runs"]}
if any(runs.get(n, {}).get("status") != "completed" for n in wanted):
    print("pending")
elif all(runs[n]["conclusion"] == "success" for n in wanted):
    print("success")
else:
    print("failed")
PY
) || exit 1
case "$verdict" in
  pending) report "waiting for CI"; exit 0 ;;
  failed) report "CI failed, not deployed"; exit 1 ;;
  success) ;;
  *) exit 1 ;;
esac

{
  date
  # --ff-only also "succeeds" when the target is behind HEAD, so check where HEAD ended up.
  if ! git merge -q --ff-only "$sha" || [ "$(git rev-parse HEAD)" != "$sha" ]; then
    echo "cannot fast-forward $prev to $sha"
    exit 1
  fi
  git log --oneline -1
  if out=$(python3 -m unittest discover -s tests 2>&1); then
    printf '%s\n' "$out" | tail -n 3  # dash's echo would interpret backslashes
  else
    printf '%s\n' "$out" | tail -n 30
    if git reset -q --keep "$prev"; then
      echo "tests failed on the Pi; back on $prev"
    else
      echo "tests failed on the Pi and COULD NOT go back to $prev; fix the checkout by hand"
    fi
    echo "$sha failed on the Pi" > "$SEEN"
    exit 1
  fi
  # The photo page runs all the time, so it only picks up new code when restarted.
  if ! git diff --quiet "$prev" "$sha" -- pixer_web.py pixer_upload.py; then
    # Anchored, so it matches the server itself and not a shell whose command line mentions it.
    web_re='^python3 pixer_web\.py( |$)'
    pkill -f "$web_re"
    for _ in 1 2 3 4 5 6 7 8 9 10; do  # wait for the old one to free the port
      pgrep -f "$web_re" > /dev/null || break
      sleep 1
    done
    if pgrep -f "$web_re" > /dev/null; then
      echo "the old pixer_web.py did not stop; it keeps serving the previous code"
    else
      # Same command as the @reboot line in crontab.
      nohup python3 pixer_web.py --host 10.1.7.102 --min-battery 20 >> "$HOME/pixer-web.log" 2>&1 &
      web=$!
      sleep 5
      if kill -0 "$web" 2> /dev/null; then  # the new process, not an old one that lingered
        echo "restarted pixer_web.py"
      else
        echo "pixer_web.py DID NOT restart; see ~/pixer-web.log"
      fi
    fi
  fi
} >> "$LOG" 2>&1
