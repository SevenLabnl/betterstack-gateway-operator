# Security

## Reporting

Please report vulnerabilities privately through GitHub's
[security advisories](https://github.com/SevenLabnl/betterstack-gateway-operator/security/advisories/new)
rather than as a public issue.

## What this operator can reach

Worth knowing before you install it.

**In the cluster:** cluster-wide read, and only read. `get`, `list` and `watch` on
`httproutes`, plus `list` and `watch` on `customresourcedefinitions` and `namespaces`,
which kopf needs to resolve the resource it watches. There is no write verb in its
`ClusterRole` — it never annotates, patches or deletes anything in Kubernetes. That is
possible because ownership of a monitor is recorded in Better Stack rather than on
your resources.

**Outside the cluster:** it holds a Better Stack API token with write access, and uses
it to create, update and delete monitors, and to create one monitor group. It confines
itself to the group named after `CLUSTER_NAME`; monitors outside that group are never
read as ours and never removed.

The token is the sensitive part. Scope it to the team that owns your monitors rather
than giving it account-wide rights, and project it from a secret store if you run one.
Hostnames from your routes are sent to Better Stack, which is the point, but worth
stating plainly if any of them are not public.

The container runs as uid 65532 with a read-only root filesystem, no capabilities and
no privilege escalation.
