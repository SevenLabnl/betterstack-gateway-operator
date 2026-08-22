# betterstack-gateway-operator

Keeps [Better Stack](https://betterstack.com) uptime monitors in sync with the
hostnames your cluster actually serves.

It watches Gateway API `HTTPRoute` resources, works out which hostnames deserve a
monitor, and makes Better Stack agree — creating what appeared, updating what changed,
removing what is gone. Nothing to remember, nothing to click.

```
created monitor 4845100 for https://shop.example.com/ (acme/shop)
deleted monitor 4845101 for https://old.example.com/ — no route serves it any more
```

> Built at [SevenLab](https://sevenlab.nl) and not affiliated with Better Stack. The
> name says which API it speaks to, nothing more.

## Why this exists

Every project we could find watches `Ingress`, and one watches Traefik's
`IngressRoute`. None watches `HTTPRoute`. Ingress is frozen, our clusters have none
left, and "add Gateway API support" was not a feature we could file upstream — it was
the whole thing.

| Project | Provider | Watches |
|---|---|---|
| [luong-komorebi/betterstack-monitor-controller](https://github.com/luong-komorebi/betterstack-monitor-controller) | Better Stack | Ingress |
| [PDOK/betterstack-gateway-operator](https://github.com/PDOK/betterstack-gateway-operator) | Pingdom, Better Stack | Traefik IngressRoute |
| [stakater/IngressMonitorController](https://github.com/stakater/IngressMonitorController) | UptimeRobot, Pingdom, StatusCake, … | Ingress |

We took the shape of the idea from them and changed two things.

**Ownership lives in Better Stack, not in your cluster.** The others write monitor IDs
back onto the Ingress as an annotation. That needs write access to resources owned by
whoever deployed the app, and it breaks the moment someone recreates the resource
without carrying the annotation across. Instead every monitor goes into one Better
Stack *monitor group* named after the cluster, and that group is the record of what we
own. The operator therefore needs no write access to anything in Kubernetes — its
`ClusterRole` is read-only: `httproutes`, plus the `customresourcedefinitions` and
`namespaces` that kopf reads to resolve what it is watching.

A monitor you made by hand, outside that group, is never touched.

**Hostnames are selected by rule, not by annotation.** Requiring an annotation on every
route means a pull request against every repository that defines one, for a change none
of those teams care about. Instead you say once which hostname suffixes to skip —
normally the development domains — and individual routes can still opt out.

## How it decides what to monitor

For each `HTTPRoute`, every hostname in `spec.hostnames` is considered:

1. `betterstack.sevenlab.nl/enabled: "false"` on the route excludes all of its hostnames.
2. `betterstack.sevenlab.nl/enabled: "true"` includes them, whatever rule 3 says.
3. Otherwise a hostname is monitored unless it matches `EXCLUDE_SUFFIXES`.
4. A wildcard hostname (`*.example.com`) is never monitored — there is no address to
   request, and inventing one that happens to 404 is worse than not checking.

Two routes claiming the same hostname and path — an HTTP redirect route and the HTTPS
one usually do — produce one monitor, not two.

## Installing

```bash
kubectl apply -f https://raw.githubusercontent.com/SevenLabnl/betterstack-gateway-operator/v0.4.0/deploy/operator.yaml
```

Then the two things that differ per cluster, which are not in that file because one of
them is a secret — see [`deploy/`](./deploy) for the full walkthrough:

```bash
kubectl create configmap betterstack-gateway-operator-cluster -n betterstack-gateway-operator \
  --from-literal=CLUSTER_NAME=my-cluster

kubectl create secret generic betterstack-gateway-operator-token -n betterstack-gateway-operator \
  --from-literal=token=<token>
```

It ships with `DRY_RUN: "true"`. Read the plan it logs, then turn it off.

### The token

A Better Stack **Uptime API token with write access**. Read-only is not enough: the
operator creates, updates and deletes monitors, and creates the monitor group on first
run if it does not exist yet.

It never touches anything outside its own monitor group, so scoping the token to the
team that owns your monitors is enough — it does not need account-wide rights.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BETTERSTACK_TOKEN` | — | **required** |
| `CLUSTER_NAME` | — | **required**, names the monitor group; must be unique per cluster |
| `EXCLUDE_SUFFIXES` | `""` | comma-separated hostname suffixes never monitored |
| `CHECK_FREQUENCY` | `180` | seconds between checks |
| `REQUEST_TIMEOUT` | `30` | seconds; Better Stack allows 2, 3, 5, 10, 15, 30, 45, 60 |
| `EXPECTED_STATUS_CODES` | `200,201,202,204` | what counts as up |
| `REGIONS` | `eu` | comma-separated, from `us,eu,as,au` |
| `RESYNC_SECONDS` | `900` | full reconcile interval |
| `BETTERSTACK_POLICY_ID` | — | escalation policy for monitors it creates; see below |
| `DRY_RUN` | `false` | log what would change, change nothing |

Bad values are rejected at startup rather than at the first API call, so a typo in a
ConfigMap fails visibly instead of quietly monitoring nothing.

## Annotations

| Annotation | Effect |
|---|---|
| `betterstack.sevenlab.nl/enabled` | `"true"` monitors it whatever the rules say, `"false"` never does |
| `betterstack.sevenlab.nl/path` | path to request instead of `/` |
| `betterstack.sevenlab.nl/expected-status-codes` | comma-separated, overrides the default |
| `betterstack.sevenlab.nl/check-frequency` | seconds, overrides the default |

### Alerting is not managed

`BETTERSTACK_POLICY_ID` is applied when a monitor is created and never on an update, and
nothing else about notification is set at all — not `team_wait`, not the email, SMS,
call or push flags.

A new monitor should not start out notifying whoever the account happens to default to,
which with no escalation policy configured is the entire team. But once it exists, who
gets woken up belongs to the people carrying the pager. A reconcile that put their
change back every fifteen minutes would be worse than never setting it.

### A note on redirects

The default expects 2xx and follows redirects, so a hostname that only redirects is
checked by whether a visitor ends up somewhere that works. TLS failures still surface,
because they happen before the redirect does.

If you set 3xx codes to assert the redirect itself, following is switched off
automatically — Better Stack refuses the combination, and rightly: follow the redirect
and you never observe the status you asked for.

## Reconciliation

Watch events handle the normal case within seconds. On top of that a full resync runs
every `RESYNC_SECONDS`: it lists every route, computes the complete desired set, and
makes the monitor group match. That second loop is what makes this self-healing. A
missed watch event, a restart during a deletion, a monitor someone removed by hand in
the Better Stack UI — all of it converges on the next pass with no recovery path of
its own.

Deletion is handled there rather than by a finalizer, deliberately. A finalizer that
cannot reach Better Stack blocks the deletion of someone's route, which trades a stale
monitor for a stuck cluster. A stale monitor for a few minutes is the better failure.

## Uninstalling

Removing the operator leaves every monitor exactly where it is, on purpose. There is no
finalizer and there will not be one.

Uninstalling an operator usually means reinstalling it, moving it, or switching it off
for an afternoon — not "these hostnames no longer need watching". And the two mistakes
are not equally bad. A monitor left behind for a cluster that is gone complains loudly
and gets cleaned up the same day. Monitoring that disappears quietly is discovered
during the next outage that nobody was paged for.

The uptime history is the other half of it. It is months of data that this operator did
not create and cannot restore; a recreated monitor is a monitor with no past, which is
useless for an SLA conversation. Deleting that as a side effect of `kubectl delete
namespace` is not a trade worth making automatic.

So when you do mean it, say so: delete the monitor group named after the cluster in
Better Stack. One deliberate action, at the moment you actually intend it.

## Two clusters must not share a name

`CLUSTER_NAME` picks the monitor group, and the group is what the operator treats as
its own. Two clusters configured with the same name will each see the other's monitors
as belonging to routes that no longer exist, and delete them — then recreate their own,
then delete them again, every resync, indefinitely.

Nothing detects this, so it is worth saying plainly: one name per cluster.

## Versions

Images are published to `ghcr.io/sevenlabnl/betterstack-gateway-operator`. Releases are
git tags, and each one publishes `X.Y.Z`, `X.Y`, `X` and `latest`, so a deployment can
pin as tightly as it likes. Pre-releases (`v0.3.0-rc1`) never become `latest`. Builds
from `main` are published as `:main` and as the commit sha, and never move a released
version.

Below 1.0 the annotation names and configuration keys may still change; anything that
does will be called out in the release notes.

## Developing

```bash
pip install -e ".[dev]"
pytest -q
```

The rules and the plan are pure functions, so what to monitor and what to change are
tested without a cluster or a network. The Better Stack client is tested against a fake
transport, which is where pagination stopping early or a silently ignored payload field
would otherwise hide.

Adding a provider means implementing four methods — see
[`providers/base.py`](./src/gateway_uptime/providers/base.py). Everything about which
hostnames matter is decided before a provider is involved.

## Licence

Apache 2.0.
