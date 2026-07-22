# What's Actually Running in Staging minikube

# staging minikube 裡實際跑了什麼

## Current inventory

## 目前的元件清單

As of this writing, the `snoopy-staging` minikube cluster runs:

截至目前為止，`snoopy-staging` 這個 minikube 叢集裡跑的東西如下：

**`kube-system` namespace — Kubernetes's own control plane + system components:**

**`kube-system` namespace ── Kubernetes 自己的控制平面 + 系統元件：**

| Component 元件 | Role 角色 |
|---|---|
| `kube-apiserver` | The control-plane entry point every request goes through. 所有請求的入口，控制平面核心。 |
| `etcd` | The cluster's datastore — every Pod/Secret/ConfigMap's actual state lives here. 叢集的資料庫，所有狀態實際存放的地方。 |
| `kube-controller-manager` | Runs reconciliation loops (e.g. keeping a Deployment's replica count correct). 跑各種調節迴圈(例如維持 Deployment 該有的 Pod 數量)。 |
| `kube-scheduler` | Decides which node a new Pod lands on. 決定新 Pod 要排到哪個 node 上。 |
| `kube-proxy` | Routes traffic for Services to the right Pod IP. 負責把 Service 的流量轉發到正確的 Pod IP。 |
| `coredns` | Cluster-internal DNS, so Pods can find each other by service name. 叢集內部 DNS，讓 Pod 之間能用服務名稱互相找到。 |
| `kindnet` | The CNI plugin — gives every Pod an IP and wires up Pod-to-Pod networking. CNI 網路外掛，負責分配 Pod IP 跟打通 Pod 間網路。 |
| `storage-provisioner` | minikube's own storage provisioner — fulfills PVC requests with actual disk. minikube 自己的儲存供應器，負責把 PVC 的請求變成真正的儲存空間。 |

**`snoopy-staging` namespace — this project's own resources:**

**`snoopy-staging` namespace ── 這個專案自己的資源：**

| Component 元件 | Role 角色 |
|---|---|
| `postgres` Deployment | The shared Postgres database (used by both dev and staging). 共用的 Postgres 資料庫(dev 跟 staging 共用同一個)。 |

Note: as of this writing, only the database is deployed here — the bot itself (`snoopy` Deployment) hasn't been deployed yet, since `deploy/k8s/overlays/staging` and the base Deployment manifest don't exist in the repo yet.

備註：截至目前為止，這裡只部署了資料庫──機器人本體(`snoopy` Deployment)還沒部署上去，因為 `deploy/k8s/overlays/staging` 跟 base Deployment manifest 都還沒寫進專案裡。

## kubelet — the piece that's not in either table

## kubelet ── 兩張表都沒列進去的那一塊

Both tables above are Pod-level inventories — anything `kubectl get pods -n <namespace>` can show you. `kubelet` doesn't appear in either because it isn't a Pod: it's the agent that runs directly on the node itself, and its job is to launch, monitor, and restart every Pod on that node — including everything in the `kube-system` table above, and, for the six core control-plane components, even before the API server itself exists (via the static-pod mechanism, see below). Every node in every Kubernetes cluster (EKS, GKE, kubeadm, minikube) runs one kubelet; unlike a CNI plugin or storage provisioner, it isn't a swappable implementation choice.

上面兩張表都只是 Pod 層級的清單 —— 也就是 `kubectl get pods -n <namespace>` 看得到的東西。`kubelet` 不會出現在任何一張表裡，因為它根本不是 Pod：它是直接跑在 node 本機上的 agent，負責啟動、監控、重啟該 node 上的每一個 Pod ── 包括上面 `kube-system` 表裡的所有元件，而對其中六個核心控制平面元件來說，甚至是在 API server 本身都還不存在之前就啟動它們(透過下面會提到的 static pod 機制)。每個 Kubernetes 叢集裡的每個 node(不管 EKS、GKE、kubeadm、minikube)都跑著一個 kubelet；它不像 CNI 外掛或儲存供應器那樣是可替換的實作選擇。

## Which of these are "just normal Kubernetes"?

## 哪些是「一般 Kubernetes 都會有」的？

The six core `kube-system` components — `kube-apiserver`, `etcd`, `kube-controller-manager`, `kube-scheduler`, `kube-proxy`, `coredns` — are part of the Kubernetes architecture itself. Every conformant cluster has them, whether it's EKS, GKE, or minikube. Only the *implementation* wrapping them (how they're packaged as containers, what flags they're started with) varies by platform.

`kube-apiserver`、`etcd`、`kube-controller-manager`、`kube-scheduler`、`kube-proxy`、`coredns` 這六個核心元件是 Kubernetes 架構本身的一部分。任何符合規範的叢集都會有這六個，不管是 EKS、GKE、還是 minikube。只有「怎麼包裝成 container、用什麼參數啟動」這種**實作細節**因平台而異。

Only two things here are minikube's *own choice*, not a universal standard: `kindnet` (the CNI plugin — EKS uses AWS VPC CNI, GKE uses its own, other kubeadm clusters commonly use Calico/Cilium/Flannel) and `storage-provisioner` (EKS uses the AWS EBS CSI driver, GKE uses the GCE PD CSI driver). Every cluster needs *some* CNI and *some* storage provisioner — but which specific implementation is platform-specific.

只有兩樣東西是 minikube **自己的選擇**，不是通用標準：`kindnet`(CNI 外掛──EKS 用 AWS VPC CNI，GKE 用自己的，其他 kubeadm 叢集常見用 Calico/Cilium/Flannel)跟 `storage-provisioner`(EKS 用 AWS EBS CSI driver，GKE 用 GCE PD CSI driver)。每個叢集都需要**某種** CNI 跟**某種**儲存供應器──但具體是哪一套實作，因平台而異。

## Why minikube's podman driver is the "unusual" one

## 為什麼 minikube 的 podman driver 是「不正常」的那個

To be precise about the comparison: a "normal, no-quirks" Kubernetes setup means EKS, GKE, or `kubeadm` installed directly on a real (or fully root-privileged) Linux machine. In all of these, `kubelet` runs with genuine root privileges directly on a real Linux host — no extra wrapping layer.

先精確定義一下對比的兩邊：「一般、沒有怪癖」的 Kubernetes 設定，指的是 EKS、GKE、或是直接在一台真實(或完全具備 root 權限)的 Linux 機器上裝 kubeadm。在這些情況下，`kubelet` 都是**直接以真正的 root 權限**跑在真實的 Linux 主機上──沒有任何額外的包裝層。

This session's minikube setup is different: the "node" is simulated by a nested container running under **rootless** Podman (no root privileges) on macOS. Since kubelet normally assumes it has root (e.g. to manage volumes and cgroups), minikube automatically turns on `KubeletInUserNamespace` to let kubelet simulate having those privileges while actually running unprivileged. That simulation layer is extra, non-default machinery layered on top of standard Kubernetes — and it's where the edge-case bug (new hostPath mounts not propagating) lived.

這次的 minikube 設定不一樣：「node」是靠 macOS 上一個用 **rootless**(非 root)Podman 跑起來的巢狀 container 去模擬出來的。因為 kubelet 平常預設自己有 root 權限(例如要管理 volume 跟 cgroup)，minikube 就自動開啟 `KubeletInUserNamespace`，讓 kubelet 在實際沒有權限的情況下，模擬出「好像有權限」的效果。這層模擬機制是額外疊加在標準 Kubernetes 之上、非預設的機制──而這次踩到的邊角案例(新增的 hostPath 掛載無法正確傳遞)，就是出在這一層。

## Two things worth being precise about

## 兩件值得說清楚的事

**`KubeletInUserNamespace` is not Podman-specific.** It's a genuine, upstream Kubernetes (kubelet) feature gate for running kubelet inside *any* rootless Linux user namespace, regardless of which container engine sits underneath — it could just as well be triggered by rootless Docker. minikube's own logic is what decided to turn it on here, specifically because it detected `--driver=podman` running rootless.

**`KubeletInUserNamespace` 不是為 Podman 量身打造的。** 它是貨真價實、上游 Kubernetes(kubelet)的官方 feature gate，用途是讓 kubelet 能在**任何** rootless 的 Linux user namespace 裡運作，不管底層是哪個容器引擎──換成 rootless Docker 一樣可能觸發它。是 minikube 自己的邏輯決定要開啟它，因為它偵測到 `--driver=podman` 正在用 rootless 模式跑。

**Podman is not a Kubernetes project.** It's an independent container engine made by Red Hat, in the same category as Docker — building/running OCI containers, unrelated to the Kubernetes project or CNCF governance. It happens to have some Kubernetes-YAML-compatible convenience commands (`podman play kube`), but that's a compatibility feature, not organizational affiliation. What made this setup unusual wasn't Podman-the-technology — it's that minikube's own integration with Podman as a driver is explicitly labeled "experimental," and Podman-on-macOS-rootless is a comparatively less-traveled combination than the Docker driver most tutorials assume.

**Podman 不是 Kubernetes 的 project。** 它是 Red Hat 做的一個獨立容器引擎，跟 Docker 屬於同一類東西──負責建置/執行 OCI container，跟 Kubernetes 專案或 CNCF 治理架構完全無關。它剛好有一些跟 Kubernetes YAML 相容的便利指令(`podman play kube`)，但那只是相容性功能，不代表組織上有從屬關係。這次設定會顯得特殊，不是因為 Podman 這個技術本身有問題──而是 minikube 官方把「用 Podman 當 driver」這件事明確標記成「實驗性」，而「Podman + macOS + rootless」這個組合，比起大部分教學預設使用的 Docker driver，明顯是條比較少人走的路。
