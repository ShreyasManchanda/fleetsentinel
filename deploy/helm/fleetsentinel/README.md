FleetSentinel — same chart on kind and on the k3s demo VM.

```bash
# local (kind): datastores via compose, chart for services
helm install fs . -f values-kind.yaml
# template check without a cluster:
helm template fs . -f values-kind.yaml | kubectl apply --dry-run=client -f -

# cloud VM: render __HOSTIP__/__HOSTSLIP__ first (see infra/terraform/aws/live), then
helm install fs . -f values-vm.yaml
```

Probes: `/health` (liveness) and `/ready` (readiness; the handler returns 503 while
consumer lag exceeds the per-service threshold, so the scheduler stops sending traffic
to backlogged pods instead of killing them).
