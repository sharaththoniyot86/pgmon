# Deploying pgmon on Kubernetes (including Rancher)

pgmon ships as a self-contained container image (`Dockerfile` at the repo
root) and a Helm chart (`charts/pg-ai-monitor/`) that installs on any
Kubernetes cluster, including one managed by Rancher. For a single host
without Kubernetes, use the standalone image instead
([packaging/docker/](packaging/docker/README.md)).

## 1. Build the image

```bash
docker build -t pg-ai-monitor:1.0.0 .
```

Use a version tag rather than `:latest` for a real deployment: Kubernetes
won't re-pull a moved `:latest` on a redeploy, and you lose the ability to
roll back to a known-good version.

## 2. Get the image onto your cluster

**A. A registry the cluster can pull from** (Harbor, Nexus, ECR/GCR/ACR,
Docker Hub, GitLab/GitHub registries, ...):

```bash
docker tag pg-ai-monitor:1.0.0 <your-registry>/<your-project>/pg-ai-monitor:1.0.0
docker push <your-registry>/<your-project>/pg-ai-monitor:1.0.0
```

For a private registry, create a pull secret in the target namespace and
reference it via the chart's `image.pullSecrets` value:

```bash
kubectl create secret docker-registry pg-ai-monitor-regcred \
  --docker-server=<your-registry> --docker-username=... --docker-password=... \
  -n <your-namespace>
```

**B. Air-gapped, no shared registry** — copy the image as a file:

```bash
docker save pg-ai-monitor:1.0.0 -o pg-ai-monitor-1.0.0.tar
# copy it to the target environment, then on every node that may run the pod:
#   containerd (RKE2/K3s):  ctr -n k8s.io images import pg-ai-monitor-1.0.0.tar
#   Docker-based nodes:     docker load -i pg-ai-monitor-1.0.0.tar
```

On a multi-node cluster, pushing to a registry (option A) is the normal
answer. `airgap/build-bundle.sh` packages the image and the chart into one
tarball for carrying across an air gap.

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
purpose. `charts/pg-ai-monitor/values.yaml` documents every other value
(ingress, AI key, resource limits, the optional physical-restore
automation). `helm install` prints the next steps (creating the first
login, reaching the app).

The pod's `securityContext` passes the Kubernetes "restricted" Pod Security
Standard, which several Rancher/RKE2 clusters enforce per namespace.

### Installing from the Rancher UI

The chart ships a `questions.yaml`, so Rancher's **Apps > Charts** shows a
form (grouped fields, a masked field for the session secret and AI key, a
StorageClass picker) instead of a raw values editor. Make the chart
visible to Rancher either way:

1. **Add the repository** — **Apps > Repositories > Create**, pointing at
   this git repository (Rancher reads a chart from a path inside a git
   repo directly) or at a Helm/OCI registry you publish the chart to.
2. **Upload the chart** — `helm package charts/pg-ai-monitor` produces a
   `.tgz` that some Rancher versions accept under **Apps > Install >
   Custom**.

`image.repository`/`image.tag`, `persistence.storageClassName` and
`sessionSecret` are the fields without defaults.

## 4. First login

No admin account is baked into the image or the chart. Create one once the
pod is running:

```bash
kubectl exec -n pgmonitor deployment/pg-ai-monitor -- python -m app.create_user admin '<a-real-password>'
```

Then add the databases to monitor from the **Databases** screen
(**+ Add database**); pgmon monitors nothing by default.

## Notes

- **Ingress**: the chart's Ingress is a plain `networking.k8s.io/v1`
  Ingress. The simplest setup is a dedicated hostname with `basePath: ""`.
  To serve under a path prefix on a shared host, set `basePath` and the
  prefix-stripping annotation your ingress controller needs in
  `ingress.annotations` (nginx-ingress:
  `nginx.ingress.kubernetes.io/rewrite-target`; Traefik: a `Middleware`).
- **Backups to S3**: set the destination in the Backup & Recovery tab
  (*S3 settings*). An in-cluster MinIO/Ceph endpoint such as
  `https://minio.minio.svc.cluster.local:9000` works; paste its CA
  certificate if it's signed by a private CA. See DOCUMENTATION.md §3.4.
- **Physical/incremental restore automation** (`physicalRestore.*`) is off
  by default. When enabled it targets one named Postgres StatefulSet, and
  the chart creates a narrow Role for exactly that StatefulSet in its
  namespace. Logical (`pg_dump`) backup and restore need no configuration.
- **Monitoring CloudNativePG clusters**: register the cluster's
  `<cluster>-rw` service with a `pg_monitor` login — see
  DOCUMENTATION.md §6a for the details and what stays with the operator.
- **Extensions**: run Postgres servers on the pgmon-postgres image
  (`postgres-image/`) and every packaged extension is one click away in
  the Extensions tab.
