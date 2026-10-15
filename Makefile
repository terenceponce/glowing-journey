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
EMBEDDER_IMAGE ?= embedder:0.1.2
INGEST_IMAGE   ?= ingest:0.1.0
CHATBOT_IMAGE  ?= chatbot:0.4.0

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

# Chatbot settings (override like `make chatbot-up LLM_MODEL=glm-5.3-flash TOP_K=8`).
# Z.AI general API; not the coding-plan endpoint (/api/coding/paas/v4), which is limited to coding tools.
LLM_BASE_URL    ?= https://api.z.ai/api/paas/v4
LLM_MODEL       ?= glm-4.7-flash
LLM_TEMPERATURE ?= 0.2
LLM_MAX_TOKENS  ?= 1024
LLM_THINKING    ?= disabled
TOP_K           ?= 5
CHATBOT_PORT    ?= 8080

# Langfuse tracing for the chatbot (keys come from the app/langfuse-api Secret).
LANGFUSE_URL          ?= http://langfuse-web.langfuse.svc.cluster.local:3000
LANGFUSE_ENVIRONMENT  ?= lab
LANGFUSE_PROMPT_LABEL ?= production

# LLM-as-judge (docs/evals.md). Stays on the free model: never point JUDGE_MODEL at a paid one.
JUDGE_PROVIDER     ?= zai
JUDGE_BASE_URL     ?= $(LLM_BASE_URL)
JUDGE_MODEL        ?= glm-4.7-flash
JUDGE_RULE_ENABLED ?= false
JUDGE_SAMPLING     ?= 1
JUDGE_LIMIT        ?= 10
JUDGE_TRACES       ?=
JUDGE_FORCE        ?= false
JUDGE_MAX_ATTEMPTS ?= 8
JUDGE_BACKOFF_BASE ?= 2
JUDGE_BACKOFF_MAX  ?= 60
JUDGE_PAUSE        ?= 2
JUDGE_TIMEOUT      ?= 120

# Experiments (docs/experiments.md). Configs are named in scripts/run_experiment.py; all stay on the free model.
EXPERIMENT_CONFIG       ?= baseline
EXPERIMENT_RUN          ?=
EXPERIMENT_ITEMS        ?=
EXPERIMENT_PROMPT_LABEL ?= concise
EXPERIMENT_MAX_ATTEMPTS ?= 3
EXPERIMENT_BACKOFF_BASE ?= 30
EXPERIMENT_BACKOFF_MAX  ?= 180
EXPERIMENT_PAUSE        ?= 3
EXPERIMENT_TIMEOUT      ?= 900
EXPERIMENT_JUDGE        ?= true
EXPERIMENT_LLM_MAX_RETRIES  ?= 7
EXPERIMENT_LLM_BACKOFF_BASE ?= 2
EXPERIMENT_LLM_BACKOFF_MAX  ?= 60

# Failure drills (docs/failure-drills.md). Each fault has a -on and an -off target; drill-restore runs every -off.
DRILL                  ?=
COMPARE                ?= baseline
DRILL_ITEMS            ?= rag-eval-01,rag-eval-05,rag-eval-09
DRILL_JUDGE            ?= false
DRILL_LLM_RETRIES      ?= 2
DRILL_EMBED_LATENCY_MS ?= 4000
DRILL_COLLECTION       ?= recipes
DRILL_PROMPT_LABEL     ?= drill-hallucinate
DRILL_TOP_K            ?= 20
DRILL_LLM_TIMEOUT      ?= 1
DRILL_ROLLOUT          := --timeout=3m

.PHONY: help cluster-up cluster-down cluster-status \
	langfuse-prereqs langfuse-secrets langfuse-up langfuse-down langfuse-purge langfuse-status langfuse-ui langfuse-test \
	qdrant-up qdrant-down qdrant-purge qdrant-status qdrant-ui \
	embedder-image embedder-up embedder-down embedder-test \
	ingest-image ingest query \
	chatbot-secret chatbot-image chatbot-config chatbot-langfuse-setup chatbot-up chatbot-down chatbot-status \
	chatbot-logs chatbot-ui chatbot-test chatbot-trace chatbot-feedback \
	judge-config judge-setup judge \
	experiment-config experiment-setup experiment experiment-check \
	drill-run drill-report drill-check drill-restore \
	drill-qdrant-down-on drill-qdrant-down-off drill-embedder-slow-on drill-embedder-slow-off \
	drill-wrong-collection-on drill-wrong-collection-off drill-llm-timeout-on drill-llm-timeout-off \
	drill-hallucination-on drill-hallucination-off drill-token-spike-on drill-token-spike-off

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

chatbot-secret: ## Create the app/zai-api Secret from secrets/zai.env (ZAI_API_KEY=...)
	KUBECTL="$(KUBECTL)" scripts/zai-secret.sh

chatbot-image: ## Build the chatbot image and load it into kind
	docker build --platform $(PLATFORM) -t $(CHATBOT_IMAGE) services/chatbot
	kind load docker-image $(CHATBOT_IMAGE) --name $(CLUSTER_NAME)

chatbot-config: ## Write the chatbot-config ConfigMap from the Makefile variables
	$(KUBECTL) -n app create configmap chatbot-config \
		--from-literal=EMBEDDER_URL=$(EMBEDDER_URL) --from-literal=QDRANT_URL=$(QDRANT_URL) \
		--from-literal=COLLECTION=$(COLLECTION) --from-literal=TOP_K=$(TOP_K) \
		--from-literal=LLM_BASE_URL=$(LLM_BASE_URL) --from-literal=LLM_MODEL=$(LLM_MODEL) \
		--from-literal=LLM_TEMPERATURE=$(LLM_TEMPERATURE) --from-literal=LLM_MAX_TOKENS=$(LLM_MAX_TOKENS) \
		--from-literal=LLM_THINKING=$(LLM_THINKING) \
		--from-literal=LANGFUSE_BASE_URL=$(LANGFUSE_URL) --from-literal=LANGFUSE_TRACING_ENVIRONMENT=$(LANGFUSE_ENVIRONMENT) \
		--from-literal=LANGFUSE_PROMPT_LABEL=$(LANGFUSE_PROMPT_LABEL) \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -

chatbot-langfuse-setup: chatbot-image chatbot-config ## Create the GLM model prices and the rag-chat prompt in Langfuse (idempotent)
	$(KUBECTL) -n app delete job chatbot-langfuse-setup --ignore-not-found
	sed 's|image: chatbot:.*|image: $(CHATBOT_IMAGE)|' deploy/app/langfuse-setup-job.yaml | $(KUBECTL) apply -f -
	@for i in $$(seq 1 60); do \
		s=$$($(KUBECTL) -n app get job chatbot-langfuse-setup -o jsonpath='{.status.conditions[?(@.status=="True")].type}'); \
		case "$$s" in *Complete*) break;; *Failed*) $(KUBECTL) -n app logs job/chatbot-langfuse-setup --tail=30; exit 1;; esac; \
		sleep 2; \
	done
	$(KUBECTL) -n app logs job/chatbot-langfuse-setup

chatbot-up: chatbot-secret chatbot-langfuse-setup ## Build, set up Langfuse, deploy the chatbot to the app namespace and wait until Ready
	$(KUBECTL) apply -f deploy/app/chatbot.yaml
	$(KUBECTL) -n app set image deploy/chatbot chatbot=$(CHATBOT_IMAGE)
	$(KUBECTL) -n app rollout restart deploy/chatbot
	$(KUBECTL) -n app rollout status deploy/chatbot --timeout=5m

chatbot-down: ## Remove the chatbot (keeps the zai-api Secret and chatbot-config)
	$(KUBECTL) delete -f deploy/app/chatbot.yaml --ignore-not-found

chatbot-status: ## Show the chatbot pod, service and config
	@$(KUBECTL) -n app get deploy,pods,svc -l app.kubernetes.io/name=chatbot
	@$(KUBECTL) -n app get configmap chatbot-config -o jsonpath='{.data}'; echo

chatbot-logs: ## Follow the chatbot logs
	$(KUBECTL) -n app logs -f deploy/chatbot

chatbot-ui: ## Port-forward the chat page to http://localhost:8080 (CHATBOT_PORT)
	$(KUBECTL) -n app port-forward svc/chatbot $(CHATBOT_PORT):8000

SESSION ?= make-test
chatbot-test: ## Ask Q through POST /chat from inside the cluster (same SESSION = same conversation) and print the answer
	$(KUBECTL) -n app run chatbot-test --rm -i --restart=Never --quiet --image=$(CHATBOT_IMAGE) --env=Q="$(Q)" --env=SESSION="$(SESSION)" -- \
		python -c "import json, os, urllib.request as u; \
		r = u.Request('http://chatbot:8000/chat', json.dumps({'session_id': os.environ['SESSION'], 'user_id': 'make-test', \
		'message': os.environ['Q']}).encode(), {'content-type': 'application/json'}); d = json.load(u.urlopen(r, timeout=180)); \
		print('Q:', os.environ['Q']); print('A:', d['answer']); print(); \
		[print(f\"[{s['n']}] {s['score']:.3f} {s['source']} [{s['title']} > {s['section'] or '-'}] #{s['chunk_index']}\") for s in d['sources']]; \
		print(); print('model', d['model'], 'usage', d['usage']); print('trace_id', d['trace_id'])"

TRACE ?=
chatbot-trace: ## Print a Langfuse trace as a tree (make chatbot-trace TRACE=<trace-id>, from chatbot-test or the UI)
	@test -n "$(TRACE)" || (echo "usage: make chatbot-trace TRACE=<trace-id>"; exit 1)
	$(KUBECTL) -n app exec deploy/chatbot -- python show_trace.py $(TRACE)

VOTE    ?= up
COMMENT ?=
chatbot-feedback: ## Rate a turn like the chat page does (make chatbot-feedback TRACE=<id> VOTE=up|down COMMENT="...")
	@test -n "$(TRACE)" || (echo "usage: make chatbot-feedback TRACE=<trace-id> VOTE=up|down [COMMENT=...]"; exit 1)
	@case "$(VOTE)" in up|down) ;; *) echo "VOTE must be up or down"; exit 1;; esac
	$(KUBECTL) -n app exec deploy/chatbot -- env TRACE="$(TRACE)" VALUE="$(if $(filter up,$(VOTE)),1,0)" COMMENT="$(COMMENT)" \
		python -c "import json, os, urllib.request as u; \
		r = u.Request('http://localhost:8000/feedback', json.dumps({'trace_id': os.environ['TRACE'], 'value': int(os.environ['VALUE']), \
		'comment': os.environ['COMMENT'] or None, 'user_id': 'make-test', 'source': 'make'}).encode(), {'content-type': 'application/json'}); \
		print(u.urlopen(r, timeout=30).read().decode())"
	$(KUBECTL) -n app exec deploy/chatbot -- python show_trace.py --scores $(TRACE) $(if $(filter up,$(VOTE)),1,0) "$(COMMENT)"

judge-config: ## Write the judge-config ConfigMap and the judge-scripts ConfigMap (scripts/judge*.py)
	$(KUBECTL) -n app create configmap judge-config \
		--from-literal=JUDGE_PROVIDER=$(JUDGE_PROVIDER) --from-literal=JUDGE_BASE_URL=$(JUDGE_BASE_URL) \
		--from-literal=JUDGE_MODEL=$(JUDGE_MODEL) --from-literal=JUDGE_RULE_ENABLED=$(JUDGE_RULE_ENABLED) \
		--from-literal=JUDGE_SAMPLING=$(JUDGE_SAMPLING) --from-literal=JUDGE_LIMIT=$(JUDGE_LIMIT) \
		--from-literal=JUDGE_TRACES=$(JUDGE_TRACES) --from-literal=JUDGE_FORCE=$(JUDGE_FORCE) \
		--from-literal=JUDGE_MAX_ATTEMPTS=$(JUDGE_MAX_ATTEMPTS) \
		--from-literal=JUDGE_BACKOFF_BASE=$(JUDGE_BACKOFF_BASE) --from-literal=JUDGE_BACKOFF_MAX=$(JUDGE_BACKOFF_MAX) \
		--from-literal=JUDGE_PAUSE=$(JUDGE_PAUSE) --from-literal=JUDGE_TIMEOUT=$(JUDGE_TIMEOUT) \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n app create configmap judge-scripts --from-file=scripts/judge_prompts.py \
		--from-file=scripts/judge_setup.py --from-file=scripts/judge.py \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -

judge-setup: chatbot-secret judge-config ## Langfuse LLM connection (key from secrets/zai.env), evaluators and rule (JUDGE_RULE_ENABLED=true to turn it on)
	$(KUBECTL) -n app delete job judge-setup --ignore-not-found
	sed 's|image: chatbot:.*|image: $(CHATBOT_IMAGE)|' deploy/judge/setup-job.yaml | $(KUBECTL) apply -f -
	@for i in $$(seq 1 60); do \
		s=$$($(KUBECTL) -n app get job judge-setup -o jsonpath='{.status.conditions[?(@.status=="True")].type}'); \
		case "$$s" in *Complete*) break;; *Failed*) $(KUBECTL) -n app logs job/judge-setup --tail=30; exit 1;; esac; \
		sleep 2; \
	done
	$(KUBECTL) -n app logs job/judge-setup

judge: chatbot-secret judge-config ## Script judge: score the latest JUDGE_LIMIT chat turns (or JUDGE_TRACES=id,id), resumable; JUDGE_FORCE=true re-scores
	$(KUBECTL) -n app delete job judge --ignore-not-found
	sed 's|image: chatbot:.*|image: $(CHATBOT_IMAGE)|' deploy/judge/judge-job.yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n app wait --for=condition=Ready pod -l job-name=judge --timeout=2m || true
	$(KUBECTL) -n app logs -f job/judge
	@s=$$($(KUBECTL) -n app get job judge -o jsonpath='{.status.conditions[?(@.status=="True")].type}'); \
	case "$$s" in *Failed*) echo "judge Job failed: $$s"; exit 1;; esac

experiment-config: judge-config ## Write the experiment-config ConfigMap and the experiment-scripts ConfigMap (scripts/experiment*.py, run_experiment.py, judge*.py)
	$(KUBECTL) -n app create configmap experiment-config \
		--from-literal=EXPERIMENT_CONFIG=$(EXPERIMENT_CONFIG) --from-literal=EXPERIMENT_RUN=$(EXPERIMENT_RUN) \
		--from-literal=EXPERIMENT_ITEMS=$(EXPERIMENT_ITEMS) --from-literal=EXPERIMENT_PROMPT_LABEL=$(EXPERIMENT_PROMPT_LABEL) \
		--from-literal=EXPERIMENT_MAX_ATTEMPTS=$(EXPERIMENT_MAX_ATTEMPTS) \
		--from-literal=EXPERIMENT_BACKOFF_BASE=$(EXPERIMENT_BACKOFF_BASE) --from-literal=EXPERIMENT_BACKOFF_MAX=$(EXPERIMENT_BACKOFF_MAX) \
		--from-literal=EXPERIMENT_PAUSE=$(EXPERIMENT_PAUSE) --from-literal=EXPERIMENT_TIMEOUT=$(EXPERIMENT_TIMEOUT) \
		--from-literal=EXPERIMENT_JUDGE=$(EXPERIMENT_JUDGE) \
		--from-literal=EXPERIMENT_LLM_MAX_RETRIES=$(EXPERIMENT_LLM_MAX_RETRIES) \
		--from-literal=EXPERIMENT_LLM_BACKOFF_BASE=$(EXPERIMENT_LLM_BACKOFF_BASE) \
		--from-literal=EXPERIMENT_LLM_BACKOFF_MAX=$(EXPERIMENT_LLM_BACKOFF_MAX) \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n app create configmap experiment-scripts --from-file=scripts/experiment_dataset.py \
		--from-file=scripts/experiment_setup.py --from-file=scripts/run_experiment.py \
		--from-file=scripts/judge.py --from-file=scripts/judge_prompts.py \
		--dry-run=client -o yaml | $(KUBECTL) apply -f -

experiment-setup: experiment-config ## Create or update the rag-eval dataset and the candidate prompt version (label EXPERIMENT_PROMPT_LABEL) in Langfuse
	$(KUBECTL) -n app delete job experiment-setup --ignore-not-found
	sed 's|image: chatbot:.*|image: $(CHATBOT_IMAGE)|' deploy/experiments/setup-job.yaml | $(KUBECTL) apply -f -
	@for i in $$(seq 1 60); do \
		s=$$($(KUBECTL) -n app get job experiment-setup -o jsonpath='{.status.conditions[?(@.status=="True")].type}'); \
		case "$$s" in *Complete*) break;; *Failed*) $(KUBECTL) -n app logs job/experiment-setup --tail=30; exit 1;; esac; \
		sleep 2; \
	done
	$(KUBECTL) -n app logs job/experiment-setup

experiment: chatbot-secret experiment-config ## Run the dataset under EXPERIMENT_CONFIG as dataset run EXPERIMENT_RUN (default: the config name), judge it; resumable
	$(KUBECTL) -n app delete job experiment --ignore-not-found
	sed 's|image: chatbot:.*|image: $(CHATBOT_IMAGE)|' deploy/experiments/experiment-job.yaml | $(KUBECTL) apply -f -
	$(KUBECTL) -n app wait --for=condition=Ready pod -l job-name=experiment --timeout=2m || true
	$(KUBECTL) -n app logs -f job/experiment
	@s=$$($(KUBECTL) -n app get job experiment -o jsonpath='{.status.conditions[?(@.status=="True")].type}'); \
	case "$$s" in *Failed*) echo "experiment Job failed: $$s"; exit 1;; esac

experiment-check: ## Check the defaults are back (top-k, prompt label, model, chunking, production prompt) and that the chatbot answers
	@ingested=$$($(KUBECTL) -n app get configmap ingest-config -o jsonpath='{.data.CHUNK_SIZE}/{.data.CHUNK_OVERLAP}'); \
	$(KUBECTL) -n app exec -i deploy/chatbot -- python - $(TOP_K) $(LANGFUSE_PROMPT_LABEL) $(LLM_MODEL) \
		"$$ingested" $(CHUNK_SIZE)/$(CHUNK_OVERLAP) < scripts/experiment_check.py
	$(MAKE) --no-print-directory chatbot-test Q="What is a Pod?" SESSION=experiment-check

# ---- Failure drills (docs/failure-drills.md) ----
# Faults live in the cluster (a scaled-down Qdrant, an env var on a Deployment), never in the chatbot's code paths
# for normal use. -on switches one on, -off restores it; both are safe to repeat.

drill-run: ## Send the drill's traffic: DRILL_ITEMS through the runner as dataset run drill-$(DRILL), no overrides (DRILL=<name>)
	@test -n "$(DRILL)" || (echo "usage: make drill-run DRILL=<name> [DRILL_ITEMS=...] [DRILL_JUDGE=true]"; exit 1)
	$(MAKE) --no-print-directory experiment EXPERIMENT_CONFIG=as-deployed EXPERIMENT_RUN=drill-$(DRILL) \
		EXPERIMENT_ITEMS=$(DRILL_ITEMS) EXPERIMENT_JUDGE=$(DRILL_JUDGE) EXPERIMENT_MAX_ATTEMPTS=1 \
		EXPERIMENT_LLM_MAX_RETRIES=$(DRILL_LLM_RETRIES) EXPERIMENT_LLM_BACKOFF_BASE=2 EXPERIMENT_LLM_BACKOFF_MAX=8

drill-report: ## Read the drill's traces back from Langfuse, next to a reference run (DRILL=<name> COMPARE=baseline)
	@test -n "$(DRILL)" || (echo "usage: make drill-report DRILL=<name> [COMPARE=baseline]"; exit 1)
	$(KUBECTL) -n app exec -i deploy/chatbot -- python - drill-$(DRILL) $(COMPARE) < scripts/drill_report.py

drill-qdrant-down-on: ## Drill: scale Qdrant to 0 replicas
	$(KUBECTL) -n qdrant scale statefulset/qdrant --replicas=0
	$(KUBECTL) -n qdrant wait --for=delete pod/qdrant-0 --timeout=2m

drill-qdrant-down-off: ## Restore: Qdrant back to 1 replica (its PVC kept the collections)
	$(KUBECTL) -n qdrant scale statefulset/qdrant --replicas=1
	$(KUBECTL) -n qdrant rollout status statefulset/qdrant $(DRILL_ROLLOUT)

drill-embedder-slow-on: ## Drill: add DRILL_EMBED_LATENCY_MS to every embedder request (needs make embedder-up once, image 0.1.2)
	@test "$$($(KUBECTL) -n app get deploy/embedder -o jsonpath='{.spec.template.spec.containers[0].image}')" = "$(EMBEDDER_IMAGE)" \
		|| (echo "the embedder isn't running $(EMBEDDER_IMAGE): run make embedder-up first"; exit 1)
	$(KUBECTL) -n app set env deploy/embedder EMBED_LATENCY_MS=$(DRILL_EMBED_LATENCY_MS)
	$(KUBECTL) -n app rollout status deploy/embedder $(DRILL_ROLLOUT)

drill-embedder-slow-off: ## Restore: no artificial latency in the embedder
	$(KUBECTL) -n app set env deploy/embedder EMBED_LATENCY_MS=0
	$(KUBECTL) -n app rollout status deploy/embedder $(DRILL_ROLLOUT)

drill-wrong-collection-on: ## Drill: seed the unrelated collection DRILL_COLLECTION and point the chatbot at it
	$(KUBECTL) -n app exec -i deploy/chatbot -- python - seed $(DRILL_COLLECTION) < scripts/drill_collection.py
	$(KUBECTL) -n app set env deploy/chatbot COLLECTION=$(DRILL_COLLECTION)
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)

drill-wrong-collection-off: ## Restore: the chatbot back on its configured collection, the drill collection deleted
	$(KUBECTL) -n app set env deploy/chatbot COLLECTION-
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)
	$(KUBECTL) -n app exec -i deploy/chatbot -- python - delete $(DRILL_COLLECTION) < scripts/drill_collection.py

drill-llm-timeout-on: ## Drill: the chatbot's LLM timeout drops to DRILL_LLM_TIMEOUT seconds, shorter than any real answer
	$(KUBECTL) -n app set env deploy/chatbot LLM_TIMEOUT=$(DRILL_LLM_TIMEOUT)
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)

drill-llm-timeout-off: ## Restore: the chatbot's default LLM timeout (60 s)
	$(KUBECTL) -n app set env deploy/chatbot LLM_TIMEOUT-
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)

drill-hallucination-on: ## Drill: a prompt version that tells the model to always answer, under label DRILL_PROMPT_LABEL (never production), served by the chatbot
	$(KUBECTL) -n app exec -i deploy/chatbot -- python - on $(DRILL_PROMPT_LABEL) < scripts/drill_prompt.py
	$(KUBECTL) -n app set env deploy/chatbot LANGFUSE_PROMPT_LABEL=$(DRILL_PROMPT_LABEL)
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)

drill-hallucination-off: ## Restore: the chatbot back on production, the drill label removed
	$(KUBECTL) -n app set env deploy/chatbot LANGFUSE_PROMPT_LABEL-
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)
	$(KUBECTL) -n app exec -i deploy/chatbot -- python - off $(DRILL_PROMPT_LABEL) < scripts/drill_prompt.py

drill-token-spike-on: ## Drill: the chatbot retrieves DRILL_TOP_K chunks per question instead of 5
	$(KUBECTL) -n app set env deploy/chatbot TOP_K=$(DRILL_TOP_K)
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)

drill-token-spike-off: ## Restore: TOP_K back to the configured value
	$(KUBECTL) -n app set env deploy/chatbot TOP_K-
	$(KUBECTL) -n app rollout status deploy/chatbot $(DRILL_ROLLOUT)

drill-restore: drill-qdrant-down-off drill-embedder-slow-off drill-llm-timeout-off drill-token-spike-off drill-wrong-collection-off drill-hallucination-off ## Switch every fault off
	@echo "all drill faults are off; run make drill-check"

drill-check: ## No fault left (no env overrides, Qdrant and embedder up), defaults back, and the chatbot answers
	@echo "chatbot env overrides (expected: only ZAI_API_KEY):"; \
	$(KUBECTL) -n app get deploy/chatbot -o jsonpath='{range .spec.template.spec.containers[0].env[*]}  {.name}{"\n"}{end}'
	@echo "embedder EMBED_LATENCY_MS: $$($(KUBECTL) -n app get deploy/embedder -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="EMBED_LATENCY_MS")].value}')"
	@echo "qdrant ready replicas: $$($(KUBECTL) -n qdrant get statefulset/qdrant -o jsonpath='{.status.readyReplicas}')"
	@$(MAKE) --no-print-directory experiment-check
