# Deploying

Two things are not in `operator.yaml` because they differ per cluster and one of them
is a secret.

**The cluster name** decides which Better Stack monitor group this operator owns. Two
clusters sharing a name would delete each other's monitors on the first resync, so the
operator refuses to start without it.

```bash
kubectl create configmap betterstack-gateway-operator-cluster -n betterstack-gateway-operator \
  --from-literal=CLUSTER_NAME=sevenlab-production-cluster
```

**The token** is a Better Stack Uptime API token.

```bash
kubectl create secret generic betterstack-gateway-operator-token -n betterstack-gateway-operator \
  --from-literal=token=<token>
```

If you run External Secrets, project it from your secret store instead of creating it
by hand — the key must be `token`.

Then:

```bash
kubectl apply -f operator.yaml
```

## Start in dry run

`DRY_RUN` is `"true"` in the shipped ConfigMap on purpose. The operator will log the
plan it would carry out and touch nothing:

```
[dry-run] create https://shop.example.com/ (acme/shop)
[dry-run] delete https://old.example.com/
```

Read that list. It is the whole behaviour of the thing, visible before it acts. When it
says what you expect:

```bash
kubectl set env deploy/betterstack-gateway-operator -n betterstack-gateway-operator DRY_RUN=false
```

A deletion in that list is worth a second look: it means a monitor exists in the group
that no HTTPRoute accounts for. Usually that is a hostname that genuinely went away.
Occasionally it is a route that has not been applied yet, and letting the operator run
would remove a monitor you wanted to keep.

## What it needs

Cluster-wide `get`, `list` and `watch` on `httproutes`. No write access anywhere — see
the comment in `operator.yaml` for why that is possible.
