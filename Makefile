CLUSTER_NAME ?= obs-lab
KIND_CONFIG  ?= cluster/kind-config.yaml
KUBE_CONTEXT := kind-$(CLUSTER_NAME)
KUBECTL      := kubectl --context $(KUBE_CONTEXT)

.PHONY: help cluster-up cluster-down cluster-status

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
