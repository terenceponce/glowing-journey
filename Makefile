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

.PHONY: help cluster-up cluster-down cluster-status \
	langfuse-prereqs langfuse-secrets langfuse-up langfuse-down langfuse-purge langfuse-status langfuse-ui langfuse-test \
	qdrant-up qdrant-down qdrant-purge qdrant-status qdrant-ui

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
