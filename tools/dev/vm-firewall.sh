#!/usr/bin/env bash
# The firewall rules of the public VM (Stage 11), from tools/dev/vm-firewall.allow: creates a rule
# that is missing, updates one whose ports or sources differ, and touches nothing else. A rule is
# this script's only if its description carries the mark below; a rule of the same name without the
# mark stops the run. Every rule applies to the network tag of the VM only.
#   tools/dev/vm-firewall.sh            apply the allowlist, then list every rule of the network
#   tools/dev/vm-firewall.sh --check    list what would change; change nothing
# Removing a line does not delete its rule: delete it by hand (gcloud compute firewall-rules delete).
#   ERGPLAN_PROJECT (the metadata server's, else gcloud's) · ERGPLAN_TAG (replan2) · ERGPLAN_NETWORK (default)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LIST="${ERGPLAN_FIREWALL_LIST:-$HERE/vm-firewall.allow}"
MARK="ergPLAN stage 11 (tools/dev/vm-firewall.sh)"
TAG="${ERGPLAN_TAG:-replan2}"
NET="${ERGPLAN_NETWORK:-default}"
CHECK=0; [ "${1:-}" = --check ] && CHECK=1
die() { printf 'STOPPED: %s\n' "$*" >&2; exit 1; }
PROJECT="${ERGPLAN_PROJECT:-$(curl -sf -m 3 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/project/project-id 2>/dev/null \
  || gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] || die "no project: set ERGPLAN_PROJECT"
[ -f "$LIST" ] || die "no allowlist at $LIST"

norm() { tr ',' '\n' | sed '/^$/d' | sort | paste -sd, -; }  # order-free comparison of a list

while read -r name allow sources extra; do
  case "$name" in ''|'#'*) continue ;; esac
  [ -z "${extra:-}" ] && [ -n "${sources:-}" ] || die "$name: expected 'name protocol:ports sources'"
  case "$name" in replan2-allow-*) ;; *) die "$name: a rule name must start with replan2-allow-" ;; esac
  if cur="$(gcloud compute firewall-rules describe "$name" --project "$PROJECT" \
      --format='value(description,allowed[].map().firewall_rule().list(),sourceRanges.list(),targetTags.list(),network.basename(),direction)' 2>/dev/null)"; then
    IFS=$'\t' read -r c_desc c_allow c_src c_tags c_net c_dir <<<"$cur"
    [ "$c_desc" = "$MARK" ] || die "$name exists and was not created by this script; it is left alone"
    if [ "$(echo "$c_allow" | tr ';' ',' | norm)" = "$(echo "$allow" | norm)" ] \
        && [ "$(echo "$c_src" | tr ';' ',' | norm)" = "$(echo "$sources" | norm)" ] \
        && [ "$c_tags" = "$TAG" ] && [ "$c_net" = "$NET" ] && [ "$c_dir" = INGRESS ]; then
      echo "ok       $name  $allow  from $sources"
    elif [ "$CHECK" = 1 ]; then
      echo "differs  $name  now $c_allow from $c_src  →  $allow from $sources"
    else
      gcloud compute firewall-rules update "$name" --project "$PROJECT" --allow "$allow" \
        --source-ranges "$sources" --target-tags "$TAG" --quiet >/dev/null
      echo "updated  $name  $allow  from $sources"
    fi
  elif [ "$CHECK" = 1 ]; then
    echo "missing  $name  $allow  from $sources"
  else
    gcloud compute firewall-rules create "$name" --project "$PROJECT" --network "$NET" \
      --direction INGRESS --priority 1000 --action allow --rules "$allow" \
      --source-ranges "$sources" --target-tags "$TAG" --description "$MARK" --quiet >/dev/null
    echo "created  $name  $allow  from $sources"
  fi
done < "$LIST"

echo
echo "Every rule of network $NET (rules without this script's mark are never changed here):"
gcloud compute firewall-rules list --project "$PROJECT" --filter="network~/$NET\$" --sort-by=priority \
  --format='table(name,priority,sourceRanges.list():label=FROM,allowed[].map().firewall_rule().list():label=ALLOW,denied[].map().firewall_rule().list():label=DENY,targetTags.list():label=TAGS,description)'
