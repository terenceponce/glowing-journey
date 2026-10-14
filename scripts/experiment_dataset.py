"""The evaluation dataset for the RAG chatbot experiments (docs/experiments.md): 20 questions about the corpus,
the Kubernetes concepts docs (docs/vector-store.md).

Each item has:
  id        stable, so `make experiment-setup` updates items instead of adding new ones
  kind      direct: a standalone question; follow-up: asked after a fixed earlier turn (`history`);
            off-topic: not covered by the corpus, the right answer is "I don't know from the documentation"
  question  what the chatbot is asked
  history   for follow-ups: the earlier turn, sent as fixed history so only the follow-up varies between configs
  answer    the expected answer, written from the docs
  sources   the expected source pages (`source` payload in Qdrant, without the corpus prefix); the first is the
            main one. Retrieval counts as a hit when any of them is among the retrieved chunks. Empty for off-topic.

Langfuse stores `question` and `history` as the item input, `answer` and `sources` as the expected output,
and `kind` as metadata.
"""

DATASET = "rag-eval"
SOURCE_PREFIX = "content/en/docs/concepts/"

ITEMS = [
    {
        "id": "rag-eval-01",
        "kind": "direct",
        "question": "What does a ReplicaSet do?",
        "answer": "A ReplicaSet maintains a stable set of replica Pods running at any given time, so a specified "
        "number of identical Pods is available. It creates or deletes Pods to reach the desired count. Usually "
        "you use a Deployment, which manages ReplicaSets, instead of using them directly.",
        "sources": ["workloads/controllers/replicaset.md"],
    },
    {
        "id": "rag-eval-02",
        "kind": "direct",
        "question": "How can I make sure a copy of a Pod runs on every node, for example a log collector?",
        "answer": "Use a DaemonSet. It ensures that all (or some, selected) nodes run a copy of a Pod; Pods are "
        "added as nodes join the cluster and garbage collected when nodes are removed. Typical uses are "
        "cluster storage, log collection and node monitoring daemons.",
        "sources": ["workloads/controllers/daemonset.md"],
    },
    {
        "id": "rag-eval-03",
        "kind": "direct",
        "question": "How do I run a Job on a schedule, like every night at 2 AM?",
        "answer": "Use a CronJob. It creates Jobs on a repeating schedule written in Cron format in "
        ".spec.schedule, e.g. \"0 2 * * *\" for 2 AM every day. The Job to run is defined in .spec.jobTemplate.",
        "sources": ["workloads/controllers/cron-jobs.md"],
    },
    {
        "id": "rag-eval-04",
        "kind": "direct",
        "question": "How can finished Jobs be cleaned up automatically?",
        "answer": "Set .spec.ttlSecondsAfterFinished on the Job. The TTL-after-finished controller deletes the Job, "
        "and its dependent objects such as Pods, that many seconds after it finishes (Complete or Failed).",
        "sources": ["workloads/controllers/ttlafterfinished.md", "workloads/controllers/job.md"],
    },
    {
        "id": "rag-eval-05",
        "kind": "direct",
        "question": "What are init containers and how are they different from regular containers?",
        "answer": "Init containers run before the app containers in a Pod starts. Each runs to completion, one after "
        "the other, and each must succeed before the next starts; if one fails, the kubelet restarts it until it "
        "succeeds (unless restartPolicy is Never). Unlike regular containers they don't support lifecycle hooks or "
        "liveness, readiness and startup probes, and they can contain setup tools not in the app image.",
        "sources": ["workloads/pods/init-containers.md"],
    },
    {
        "id": "rag-eval-06",
        "kind": "direct",
        "question": "What is the difference between liveness, readiness and startup probes?",
        "answer": "A liveness probe decides when to restart a container (e.g. a deadlock). A readiness probe decides "
        "when a container is ready to accept traffic; a Pod that isn't ready is removed from Service endpoints. A "
        "startup probe checks whether the application has started; until it succeeds, liveness and readiness "
        "probes are disabled, which protects slow-starting containers.",
        "sources": ["workloads/pods/probes.md", "workloads/pods/pod-lifecycle.md"],
    },
    {
        "id": "rag-eval-07",
        "kind": "direct",
        "question": "What are the Pod QoS classes and which Pods are evicted first when a node runs out of resources?",
        "answer": "The QoS classes are Guaranteed, Burstable and BestEffort, assigned from the containers' resource "
        "requests and limits. Under node resource pressure, BestEffort Pods are evicted first, then Burstable, and "
        "Guaranteed Pods last.",
        "sources": ["workloads/pods/pod-qos.md", "scheduling-eviction/node-pressure-eviction.md"],
    },
    {
        "id": "rag-eval-08",
        "kind": "direct",
        "question": "What is a PodDisruptionBudget for?",
        "answer": "A PodDisruptionBudget limits how many Pods of a replicated application can be down at the same "
        "time from voluntary disruptions, such as node drains. It sets minAvailable or maxUnavailable; the "
        "Eviction API respects it, but it doesn't protect against involuntary disruptions like node failures.",
        "sources": ["workloads/pods/disruptions.md"],
    },
    {
        "id": "rag-eval-09",
        "kind": "direct",
        "question": "How do taints and tolerations work, and what does the NoExecute effect do?",
        "answer": "A taint on a node repels Pods that don't tolerate it; a toleration on a Pod lets the scheduler "
        "place it on nodes with matching taints. Effects are NoSchedule, PreferNoSchedule and NoExecute. NoExecute "
        "also evicts Pods already running on the node that don't tolerate the taint (optionally after "
        "tolerationSeconds).",
        "sources": ["scheduling-eviction/taint-and-toleration.md"],
    },
    {
        "id": "rag-eval-10",
        "kind": "direct",
        "question": "What access modes can a PersistentVolume have?",
        "answer": "ReadWriteOnce (read-write by a single node), ReadOnlyMany (read-only by many nodes), "
        "ReadWriteMany (read-write by many nodes) and ReadWriteOncePod (read-write by a single Pod).",
        "sources": ["storage/persistent-volumes.md"],
    },
    {
        "id": "rag-eval-11",
        "kind": "direct",
        "question": "When is the data in an emptyDir volume deleted?",
        "answer": "An emptyDir volume is created empty when the Pod is assigned to a node and lives as long as the "
        "Pod runs on that node. When the Pod is removed from the node, the data is deleted permanently. A container "
        "crash does not remove the Pod, so the data survives container crashes.",
        "sources": ["storage/volumes.md", "storage/ephemeral-volumes.md"],
    },
    {
        "id": "rag-eval-12",
        "kind": "direct",
        "question": "By default, can every Pod talk to every other Pod, and how do NetworkPolicies change that?",
        "answer": "Yes: by default Pods are non-isolated and accept all ingress and egress traffic. A Pod becomes "
        "isolated for ingress or egress once a NetworkPolicy selects it with that policy type; then only traffic "
        "allowed by some policy is permitted. Policies are additive and need a network plugin that supports them.",
        "sources": ["services-networking/network-policies.md"],
    },
    {
        "id": "rag-eval-13",
        "kind": "direct",
        "question": "What Service types are there?",
        "answer": "ClusterIP (the default; an internal cluster IP), NodePort (a static port on each node's IP), "
        "LoadBalancer (an external load balancer from the cloud provider) and ExternalName (maps the Service to a "
        "DNS name via a CNAME record).",
        "sources": ["services-networking/service.md"],
    },
    {
        "id": "rag-eval-14",
        "kind": "direct",
        "question": "What are the three Pod Security Standards levels?",
        "answer": "Privileged (unrestricted), Baseline (minimally restrictive; prevents known privilege escalations "
        "and allows the default Pod configuration) and Restricted (heavily restricted, following Pod hardening best "
        "practices).",
        "sources": ["security/pod-security-standards.md", "security/pod-security-admission.md"],
    },
    {
        "id": "rag-eval-15",
        "kind": "follow-up",
        "history": [
            {"role": "user", "content": "What is a Deployment?"},
            {
                "role": "assistant",
                "content": "A Deployment provides declarative updates for Pods and ReplicaSets: you describe a "
                "desired state, and the Deployment controller changes the actual state to match it at a "
                "controlled rate [1].",
            },
        ],
        "question": "How do I roll it back to a previous version?",
        "answer": "Use kubectl rollout undo, e.g. kubectl rollout undo deployment/nginx-deployment, to go back to "
        "the previous revision, or add --to-revision=N for a specific one. kubectl rollout history lists the "
        "revisions.",
        "sources": ["workloads/controllers/deployment.md"],
    },
    {
        "id": "rag-eval-16",
        "kind": "follow-up",
        "history": [
            {"role": "user", "content": "What is a StatefulSet?"},
            {
                "role": "assistant",
                "content": "A StatefulSet is the workload API object for managing stateful applications. Like a "
                "Deployment it manages Pods from the same spec, but it keeps a sticky identity for each Pod [1].",
            },
        ],
        "question": "Do its Pods keep the same network names when they're rescheduled?",
        "answer": "Yes. Each Pod gets a stable, unique network identity: its hostname is the StatefulSet name plus "
        "an ordinal (e.g. web-0), and a headless Service gives each Pod a stable DNS name. The identity sticks to "
        "the Pod regardless of which node it's rescheduled on.",
        "sources": ["workloads/controllers/statefulset.md"],
    },
    {
        "id": "rag-eval-17",
        "kind": "follow-up",
        "history": [
            {"role": "user", "content": "What is a Kubernetes Secret?"},
            {
                "role": "assistant",
                "content": "A Secret is an object that holds a small amount of sensitive data such as a password, "
                "a token or a key, so you don't need to put it in your application code [1].",
            },
        ],
        "question": "Are they encrypted by default?",
        "answer": "No. By default Secrets are stored unencrypted in the API server's data store (etcd); the values "
        "are only base64-encoded. Anyone with API or etcd access can read them. You should enable encryption at "
        "rest, restrict access with RBAC, and limit which containers can read them.",
        "sources": ["configuration/secret.md", "security/secrets-good-practices.md"],
    },
    {
        "id": "rag-eval-18",
        "kind": "off-topic",
        "question": "What is the capital of France?",
        "answer": "I don't know from the documentation.",
        "sources": [],
    },
    {
        "id": "rag-eval-19",
        "kind": "off-topic",
        "question": "How do I bake sourdough bread?",
        "answer": "I don't know from the documentation.",
        "sources": [],
    },
    {
        "id": "rag-eval-20",
        "kind": "off-topic",
        "question": "How much does a managed GKE cluster cost per month?",
        "answer": "I don't know from the documentation.",
        "sources": [],
    },
]
