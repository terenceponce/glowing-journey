CLUSTER_NAME ?= obs-lab
KIND_CONFIG  ?= cluster/kind-config.yaml
KUBE_CONTEXT := kind-$(CLUSTER_NAME)
KUBECTL      := kubectl --context $(KUBE_CONTEXT)
HELM         := helm --kube-context $(KUBE_CONTEXT)

# Versions from the langfuse-k8s README (chart v2 prerequisites).
LANGFUSE_CHART_VERSION      ?= 2.1.4
CERT_MANAGER_VERSION        ?= v1.20.2
CLICKHOUSE_OPERATOR_VERSION ?= 0.0.8

# Qdrant chart (https://github.com/qdrant/qdrant-helm); app version matches the chart version.
QDRANT_CHART_VERSION ?= 1.19.2

# Locally built images, loaded into kind (no registry). Bump the tag when the code changes.
PLATFORM       ?= linux/arm64
EMBEDDER_IMAGE ?= embedder:0.1.1
INGEST_IMAGE   ?= ingest:0.1.0

# Corpus baked into the ingest image: a pinned commit of kubernetes/website.
CORPUS_REPO ?= https://github.com/kubernetes/website.git
CORPUS_REF  ?= cb2e97a15992f47dba976a47ccca071e4d167464
CORPUS_PATH ?= content/en/docs/concepts

# Ingestion settings (override like `make ingest CHUNK_SIZE=800 CHUNK_OVERLAP=100`).
COLLECTION    ?= docs
CHUNK_SIZE    ?= 1200
CHUNK_OVERLAP ?= 200
EMBEDDER_URL  ?= http://embedder.app.svc.cluster.local:8000
QDRANT_URL    ?= http://qdrant.qdrant.svc.cluster.local:6333

.PHONY: help cluster-up cluster-down cluster-status \
	langfuse-prereqs langfuse-secrets langfuse-up langfuse-down langfuse-purge langfuse-status langfuse-ui langfuse-test \
	qdrant-up qdrant-down qdrant-purge qdrant-status qdrant-ui \
	embedder-image embedder-up embedder-down embedder-test \
	ingest-image ingest query

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

cluster-up: ## Create the kind cluster (if missing) and the namespaces
	@if kind get clusters 2>/dev/null | grep -qx '$(CLUSTER_NAME)'; then \
		echo "Cluster $(CLUSTER_NAME) already exists"; \
	else \
		kind create cluster --config $(KIND_CONFIG) --wait 120s; \
	fi
	$(KUBECTL) apply -f deploy/namespaces/namespaces.yaml
	$(KUBECTL) wait --for=condition=Ready node --all --timeout=120s

cluster-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER_NAME)

cluster-status: ## Show nodes, namespaces and pods
	@kind get clusters
	@$(KUBECTL) get nodes -o wide
	@$(KUBECTL) get namespaces
	@$(KUBECTL) get pods -A

langfuse-prereqs: ## Install cert-manager and the ClickHouse operator (once per cluster)
	$(HELM) upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
		--version $(CERT_MANAGER_VERSION) --namespace cert-manager --create-namespace \
		--set crds.enabled=true --wait
	$(KUBECTL) wait --for=condition=Established crd/certificates.cert-manager.io crd/issuers.cert-manager.io --timeout=120s
	$(HELM) upgrade --install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm \
		--version $(CLICKHOUSE_OPERATOR_VERSION) --namespace clickhouse-operator --create-namespace --wait
	$(KUBECTL) wait --for=condition=Established crd/clickhouseclusters.clickhouse.com crd/keeperclusters.clickhouse.com --timeout=120s

langfuse-secrets: ## Generate secrets/langfuse.env (if missing) and apply the Langfuse Secrets
	KUBECTL="$(KUBECTL)" scripts/langfuse-secrets.sh

langfuse-up: langfuse-prereqs langfuse-secrets ## Install or upgrade Langfuse and wait until it is Ready
	$(HELM) repo add langfuse https://langfuse.github.io/langfuse-k8s --force-update
	$(HELM) upgrade --install langfuse langfuse/langfuse --version $(LANGFUSE_CHART_VERSION) \
		--namespace langfuse -f deploy/langfuse/values.yaml --wait --timeout 15m
	$(KUBECTL) -n langfuse rollout status deploy/langfuse-web --timeout=10m
	$(KUBECTL) -n langfuse rollout status deploy/langfuse-worker --timeout=10m

langfuse-down: ## Uninstall Langfuse (keeps PVCs and Secrets, so data survives a reinstall)
	-$(HELM) uninstall langfuse --namespace langfuse --wait

langfuse-purge: langfuse-down ## Uninstall Langfuse and delete its data (PVCs) and Secrets
	$(KUBECTL) -n langfuse delete pvc --all
	$(KUBECTL) -n langfuse delete secret langfuse langfuse-init --ignore-not-found
	$(KUBECTL) -n app delete secret langfuse-api --ignore-not-found

langfuse-status: ## Show Langfuse pods, services and PVCs
	@$(KUBECTL) -n langfuse get pods,svc,pvc

langfuse-ui: ## Port-forward the Langfuse UI to http://localhost:3000
	$(KUBECTL) -n langfuse port-forward svc/langfuse-web 3000:3000

langfuse-test: ## Send one test trace from the app namespace with the Python SDK
	$(KUBECTL) -n app create configmap langfuse-smoke-test --from-file=scripts/langfuse_smoke_test.py \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n app delete job langfuse-smoke-test --ignore-not-found
	$(KUBECTL) apply -f deploy/langfuse/smoke-test-job.yaml
	$(KUBECTL) -n app wait --for=condition=Complete job/langfuse-smoke-test --timeout=5m || \
		($(KUBECTL) -n app logs job/langfuse-smoke-test; exit 1)
	$(KUBECTL) -n app logs job/langfuse-smoke-test

qdrant-up: ## Install or upgrade Qdrant and wait until it is Ready
	$(HELM) repo add qdrant https://qdrant.github.io/qdrant-helm --force-update
	$(HELM) upgrade --install qdrant qdrant/qdrant --version $(QDRANT_CHART_VERSION) \
		--namespace qdrant -f deploy/qdrant/values.yaml --wait --timeout 5m

qdrant-down: ## Uninstall Qdrant (keeps the PVC, so collections survive a reinstall)
	-$(HELM) uninstall qdrant --namespace qdrant --wait

qdrant-purge: qdrant-down ## Uninstall Qdrant and delete its data (PVC)
	$(KUBECTL) -n qdrant delete pvc --all

qdrant-status: ## Show Qdrant pods, services and PVCs
	@$(KUBECTL) -n qdrant get pods,svc,pvc

qdrant-ui: ## Port-forward Qdrant to http://localhost:6333/dashboard
	$(KUBECTL) -n qdrant port-forward svc/qdrant 6333:6333

embedder-image: ## Build the embedding service image (model baked in) and load it into kind
	docker build --platform $(PLATFORM) -t $(EMBEDDER_IMAGE) services/embedder
	kind load docker-image $(EMBEDDER_IMAGE) --name $(CLUSTER_NAME)

embedder-up: embedder-image ## Deploy the embedding service to the app namespace and wait until Ready
	$(KUBECTL) apply -f deploy/embedder/embedder.yaml
	$(KUBECTL) -n app set image deploy/embedder embedder=$(EMBEDDER_IMAGE)
	$(KUBECTL) -n app rollout restart deploy/embedder
	$(KUBECTL) -n app rollout status deploy/embedder --timeout=5m

embedder-down: ## Remove the embedding service
	$(KUBECTL) delete -f deploy/embedder/embedder.yaml --ignore-not-found

embedder-test: ## Call /healthz and /embed from inside the cluster
	$(KUBECTL) -n app run embedder-test --rm -i --restart=Never --quiet --image=$(EMBEDDER_IMAGE) -- \
		python -c "import json, urllib.request as u; b='http://embedder:8000'; \
		print(u.urlopen(b + '/healthz').read().decode()); \
		r = u.Request(b + '/embed', json.dumps({'texts': ['hello world']}).encode(), {'content-type': 'application/json'}); \
		d = json.load(u.urlopen(r)); print('dim', d['dim'], 'first values', d['vectors'][0][:4])"

ingest-image: ## Build the ingest image (corpus pinned to CORPUS_REF baked in) and load it into kind
	docker build --platform $(PLATFORM) -t $(INGEST_IMAGE) \
		--build-arg CORPUS_REPO=$(CORPUS_REPO) --build-arg CORPUS_REF=$(CORPUS_REF) \
		--build-arg CORPUS_PATH=$(CORPUS_PATH) services/ingest
	kind load docker-image $(INGEST_IMAGE) --name $(CLUSTER_NAME)

ingest: ingest-image ## Run the ingestion Job (recreates the Qdrant collection) and print its logs
	$(KUBECTL) -n app create configmap ingest-config \
		--from-literal=EMBEDDER_URL=$(EMBEDDER_URL) --from-literal=QDRANT_URL=$(QDRANT_URL) \
		--from-literal=COLLECTION=$(COLLECTION) --from-literal=CHUNK_SIZE=$(CHUNK_SIZE) \
		--from-literal=CHUNK_OVERLAP=$(CHUNK_OVERLAP) --from-literal=RECREATE_COLLECTION=true \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n app delete job ingest --ignore-not-found
	sed 's|image: ingest:.*|image: $(INGEST_IMAGE)|' deploy/ingest/ingest-job.yaml | $(KUBECTL) apply -f -
	@# Poll instead of `kubectl wait --for=condition=Complete`, which hangs until timeout if the Job fails.
	@for i in $$(seq 1 240); do \
		s=$$($(KUBECTL) -n app get job ingest -o jsonpath='{.status.conditions[?(@.status=="True")].type}'); \
		case "$$s" in *Complete*) break;; *Failed*) $(KUBECTL) -n app logs job/ingest --tail=30; exit 1;; esac; \
		sleep 5; \
	done
	$(KUBECTL) -n app logs job/ingest

Q ?= How do containers in the same Pod share storage?
query: ## Embed Q via the embedder and print the top-5 chunks from Qdrant (make query Q="...")
	$(KUBECTL) -n app run ingest-query --rm -i --restart=Never --quiet --image=$(INGEST_IMAGE) \
		--env=EMBEDDER_URL=$(EMBEDDER_URL) --env=QDRANT_URL=$(QDRANT_URL) --env=COLLECTION=$(COLLECTION) \
		-- python query.py "$(Q)"
