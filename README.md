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

Every hostname on every `HTTPRoute` is a candidate. What happens to it:

**By default, it is monitored.** No annotation, no list to join. A production hostname
is covered the moment a route serves it.

**Unless it matches `EXCLUDE_SUFFIXES`.** That is where development and staging domains
go. With `EXCLUDE_SUFFIXES=7dev.nl,7test.nl`, nothing on those domains is monitored.

**Unless the route says otherwise.** Two annotations override the rules, in both
directions:

```yaml
metadata:
  annotations:
    betterstack.sevenlab.nl/enabled: "true"    # monitor it even on an excluded domain
    betterstack.sevenlab.nl/enabled: "false"   # never monitor it, excluded or not
```

### Monitoring one hostname on an excluded domain

This is the common case for the second annotation. Say `7dev.nl` is excluded because it
is full of demo environments, but `argocd.7dev.nl` is a tool you actually rely on:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: argocd-server
  annotations:
    betterstack.sevenlab.nl/enabled: "true"
spec:
  hostnames:
  - argocd.7dev.nl
```

That one hostname is now monitored; the rest of `7dev.nl` still is not. The annotation
beats the suffix list, always.

Turning it off works the same way and beats everything, including a `"true"` on a
different route for the same hostname. When in doubt, `"false"` wins.

### Opting out applies to the hostname, not the route

A hostname is usually served by two routes — the real one and a companion on `:80` that
redirects to it. `enabled: "false"` on either of them stops the hostname being
monitored. You do not have to find and annotate both.

### Redirect routes

Where a real route serves a hostname, its redirect companion is ignored: it is plumbing
to get from `:80` to `:443`, not a thing to check. So one hostname means one monitor
even though two routes claim it.

A hostname served *only* by a redirect still gets one. `www.example.com` answering a
308 to the apex is the whole product there, and if it stops answering you want to know.

### Wildcards

`*.example.com` is never monitored. There is no address to request, and inventing one
that happens to 404 is worse than not checking.

## Installing

```bash
kubectl apply -f https://raw.githubusercontent.com/SevenLabnl/betterstack-gateway-operator/v0.5.0/deploy/operator.yaml
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
| `betterstack.sevenlab.nl/enabled` | `"true"` monitors the hostname even on an excluded domain, `"false"` never monitors it — see [how it decides](#how-it-decides-what-to-monitor) |
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

### Expecting a 3xx status

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

## CLUSTER_NAME, and why it matters more than it looks

`CLUSTER_NAME` is not a label. It is how the operator knows which monitors are its own.

Every monitor it creates goes into a Better Stack **monitor group** with that name,
created on first run if it does not exist. On every reconcile it reads that group back,
compares it to the hostnames the cluster currently serves, and makes the two match. So
the group is the record of ownership, and the name is the key to it.

Three things follow.

**A monitor outside the group is never touched.** Anything made by hand, or by
something else, is invisible to the operator. It will not be updated, and it will not
be deleted for having no route behind it.

**Removing the operator leaves the group alone.** See [Uninstalling](#uninstalling).

**Two clusters must never share a name.** They would share a group, and each would see
the other's monitors as belonging to routes that no longer exist. Both delete, both
recreate, every resync, forever — while the alerting silently follows whichever cluster
wrote last. Nothing detects this, and the symptom (monitors flapping in and out of
existence) does not point at the cause.

The operator refuses to start without `CLUSTER_NAME` for that reason, but it cannot
tell that a name is already in use elsewhere. Use the actual cluster name and it will
never come up.

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
