# Deploying pg-ai-monitor to another cluster (e.g. your organization's Rancher)

This app has always run as a container image (`Dockerfile` in the repo
root) -- what's new here is a Helm chart (`charts/pg-ai-monitor/`) that
makes it installable on *any* Kubernetes cluster, not just this dev k3d
one. The plain manifests under `k8s/` are what THIS specific cluster
actually runs and are hardcoded to it (namespace names, `local-path`
storage class, a Traefik-specific Ingress) -- don't copy those elsewhere.
Use the chart instead.

## 1. Build the image

```bash
docker build -t pg-ai-monitor:1.0.0 .
```

This produces a normal, self-contained image -- no dependency on k3d or
this environment. Bump the tag for anything beyond a first install (avoid
`:latest` for a real deployment: Kubernetes won't know to re-pull it on a
redeploy unless you also bump `imagePullPolicy: Always`, and then you lose
the ability to roll back to a known-good version).

## 2. Get the image onto your organization's cluster

Pick whichever applies to your environment:

**A. You have a registry your Rancher cluster can already pull from**
(Harbor, Nexus, ECR/GCR/ACR, Docker Hub, GitLab/GitHub's own registry, ...):

```bash
docker tag pg-ai-monitor:1.0.0 <your-registry>/<your-project>/pg-ai-monitor:1.0.0
docker push <your-registry>/<your-project>/pg-ai-monitor:1.0.0
```

If it's a private registry, create a pull secret in the target namespace
and reference it via the chart's `image.pullSecrets` value:

```bash
kubectl create secret docker-registry pg-ai-monitor-regcred \
  --docker-server=<your-registry> --docker-username=... --docker-password=... \
  -n <your-namespace>
```

**B. No shared registry reachable from both places (air-gapped, or the
target cluster's nodes can't reach wherever you built the image)** -- copy
the image itself as a file:

```bash
docker save pg-ai-monitor:1.0.0 -o pg-ai-monitor-1.0.0.tar
# copy pg-ai-monitor-1.0.0.tar to the target environment, then:
docker load -i pg-ai-monitor-1.0.0.tar
# ... and get it onto every node that might run the pod. How depends on
# your cluster's container runtime:
#   containerd (RKE2/K3s, most current Rancher-managed clusters):
#     ctr -n k8s.io images import pg-ai-monitor-1.0.0.tar   # run on each node
#   Docker-backed nodes: `docker load` on each node is enough.
# A single-node/dev cluster only needs this once; a real multi-node
# cluster needs it on every node the pod could be scheduled to, or push it
# to a registry instead (option A) -- that's the normal answer for
# anything with more than one node.
```

## 3. Install the chart

```bash
helm install pg-ai-monitor charts/pg-ai-monitor \
  --namespace pgmonitor --create-namespace \
  --set image.repository=<your-registry>/<your-project>/pg-ai-monitor \
  --set image.tag=1.0.0 \
  --set persistence.storageClassName=<your-storage-class> \
  --set sessionSecret="$(openssl rand -hex 32)"
```

`persistence.storageClassName` and `sessionSecret` have no default on
purpose -- see `charts/pg-ai-monitor/values.yaml` for every other value
(ingress, AI key, resource limits, the optional physical-restore RBAC) and
what each one is for. `helm install` prints exactly what to do next
(create your first login, how to reach the app) -- that's
`charts/pg-ai-monitor/templates/NOTES.txt`, tested against this cluster
end-to-end (installed, logged in, confirmed the API responds) before this
was written.

### Installing via the Rancher UI instead of the CLI

The chart ships a `questions.yaml`, so once Rancher can see it, **Apps &
Marketplace > Charts** renders a proper form (grouped fields, dropdowns,
a masked password field for the session secret and AI key, a live
StorageClass picker) instead of a raw values editor. Two ways to get
Rancher to see it:

1. **Add this repo to Rancher** -- Cluster Explorer (or the global
   "Apps" page) > **Apps > Repositories > Create**, pointing at wherever
   `charts/pg-ai-monitor/` lives: a git repo (Rancher can pull a chart from
   a specific path inside a git repo directly -- no separate chart-repo
   server needed) or a Helm repo (`index.yaml`) / OCI registry if your org
   already runs one (Harbor, ChartMuseum, Nexus). It then shows up under
   **Apps & Marketplace > Charts** like any other catalog app.
2. **No repo at all** -- some Rancher versions offer **Apps > Install >
   "Custom"**, pointing straight at a chart archive (`helm package
   charts/pg-ai-monitor` produces the `.tgz` to upload) without registering
   a repo first.

Either way, `image.repository`/`image.tag`, `persistence.storageClassName`,
and `sessionSecret` are the fields with no default -- the form's
descriptions match the comments in `charts/pg-ai-monitor/values.yaml`.
Everything else has a working default, including a container
`securityContext` hardened to pass the Kubernetes "restricted" Pod
Security Standard out of the box (several Rancher/RKE2 clusters enforce
this at the namespace level) -- verified by actually installing this chart
into a namespace with `pod-security.kubernetes.io/enforce=restricted` and
confirming the pod reaches Ready.

## 4. First login

There's no default admin account baked into the image or the chart --
pgmon starts with an empty accounts table. Create one after the pod is
running:

```bash
kubectl exec -n pgmonitor deployment/pg-ai-monitor -- python -m app.create_user admin '<a-real-password>'
```

## Things that don't carry over from this dev cluster

- **Storage class**: this cluster uses k3d's `local-path`; your
  organization's cluster almost certainly uses something else (Longhorn, a
  cloud CSI driver, etc.) -- that's `persistence.storageClassName`.
- **Ingress**: this cluster's `k8s/ingress.yaml` uses a Traefik-only CRD
  (a `Middleware` for stripping the `/pgmonitor` path prefix) to share a
  host with Rancher's own UI. The chart's Ingress template is a plain,
  controller-agnostic `networking.k8s.io/v1` Ingress instead -- the
  simplest, most portable setup is a dedicated hostname at `basePath: ""`
  (serving at `/`), which needs no path-rewrite annotation at all. If you
  do want it under a path prefix on a shared host, set `basePath` AND
  whatever prefix-stripping annotation your actual ingress controller
  needs in `ingress.annotations` (nginx-ingress:
  `nginx.ingress.kubernetes.io/rewrite-target`; Traefik: its own
  `Middleware` CRD, same idea as this cluster's) -- that part is
  controller-specific and the chart can't guess it for you.
- **The physical/incremental backup restore automation**
  (`physicalRestore.*` in values.yaml) targets one specific Postgres
  StatefulSet by name -- it's off by default in the chart. Logical
  (`pg_dump`) backup/restore, which most people actually use, needs no
  configuration either way.
- **The registered databases, extension setup (`postgres-image/`), and
  user accounts on THIS cluster are local to it** -- a fresh install
  starts empty; there's no export/import step, you just re-add databases
  via the Databases screen and re-create accounts.
