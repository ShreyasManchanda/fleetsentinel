#!/bin/bash
# Chaos: kill a processor consumer mid-burst, then prove zero-loss recovery.
# Run on the demo VM (needs kubectl + the ground-truth ledger from the simulator).
# 1. start the burst:        python tools/fs.py burst --rate 30000 --minutes 5 &
# 2. mid-burst, kill a consumer pod:
#      kubectl delete pod -l app.kubernetes.io/name=processor --grace-period=0
# 3. wait for drain, then reconcile:
#      python -m fs_batch reconcile --lake s3://fleetsentinel-lake/telemetry/ \
#        --alerts-query "SELECT vin, rule_code, event_ts, detected_ts FROM alert WHERE detected_ts > now() - interval '10 minutes'"
# PASS = consumer lag spikes then drains to 0 AND reconciliation shows 0 lost events
# (deterministic alert ids => replays dedupe; offset-named lake objects => no dupes).
set -euo pipefail
echo "killing one processor pod..."
kubectl delete pod -l app.kubernetes.io/name=processor --grace-period=0 | head -2
echo "watching lag drain (expect spike then 0)..."
kubectl exec statefulset/redpanda -- rpk group describe processor-bulk --brokers redpanda:9092 | tail -5
echo "then run: python -m fs_batch reconcile --lake <glob>"
