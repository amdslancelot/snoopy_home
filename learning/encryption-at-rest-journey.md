# etcd Encryption-at-Rest on Staging minikube: The Full Story

# staging minikube 的 etcd 加密實作全紀錄

## Why this mattered

## 為什麼要做這件事

By default, Kubernetes Secrets are stored in etcd as plain base64 — not encrypted. Base64 is an encoding, not encryption; anyone who can read etcd's raw data file can trivially decode a Secret's value. `encryption-at-rest` turns on real encryption (AES-CBC) so the actual bytes on disk are unreadable without the encryption key, even though the Kubernetes API still returns the decrypted value transparently to authorized clients.

預設情況下，Kubernetes 的 Secret 存進 etcd 時只是做 base64 編碼，並不是加密。Base64 只是編碼方式，不是加密演算法；只要有人能讀到 etcd 底層的原始資料檔，就能輕易還原出 Secret 的明文內容。`encryption-at-rest` 這個功能會啟用真正的加密(AES-CBC)，讓硬碟上實際儲存的位元組在沒有加密金鑰的情況下無法解讀，而透過 Kubernetes API 存取時，對有權限的使用者來說仍然完全透明、正常讀得到解密後的值。

## The components involved

## 涉及的元件

- **kube-apiserver** — the Kubernetes control-plane component that talks to etcd. It is the *only* component responsible for encrypting a Secret before writing it, and decrypting it on read. The encryption config has to reach this component specifically.

  **kube-apiserver** — 負責跟 etcd 溝通的控制平面元件。**只有它**負責在寫入前加密 Secret、讀取時解密。加密設定檔必須送到這個元件手上。

- **kubelet** — the per-node agent that actually launches, watches, and restarts every Pod on the node, static pods included. It's the thing that noticed the hot-edited manifest in attempt 1 below — and its reaction to that edit (silently keep running the old pod definition, or crash-loop on the new one) was the direct, observed symptom that ruled that approach out.

  **kubelet** — 每個 node 上真正負責啟動、監看、重啟該 node 上所有 Pod(static pod 也不例外)的 agent。下面「嘗試一：熱編輯 manifest」之所以會失敗，就是因為它偵測到 manifest 檔案變動後做出的反應(悄悄繼續跑舊的 Pod 定義，或是對新定義 crash-loop)——這正是實際觀察到、排除那個做法的直接症狀。

- **static pod** — kube-apiserver isn't a normal Deployment; it's a "static pod," defined by a YAML file sitting directly on the node's local disk (`/etc/kubernetes/manifests/kube-apiserver.yaml`), watched and launched directly by `kubelet` — no API server involved in bootstrapping it (chicken-and-egg: the API server can't schedule itself before it exists).

  **static pod（靜態 Pod）** — kube-apiserver 不是用一般 Deployment 部署的，而是一種「靜態 Pod」：它的定義直接寫在 node 本機的檔案(`/etc/kubernetes/manifests/kube-apiserver.yaml`)，由 `kubelet` 直接監看、直接啟動，完全不透過 API server 本身──因為雞生蛋蛋生雞，apiserver 都還沒起來，不可能靠 API 排程自己。

- **kubeadm** — the tool that bootstraps a cluster from scratch. It generates the initial `kube-apiserver.yaml`, generates TLS certs into `/var/lib/minikube/certs/`, and waits for the control plane to report healthy before finishing.

  **kubeadm** — 負責從零建立整個叢集的工具。它會產生最初的 `kube-apiserver.yaml`、把 TLS 憑證產生到 `/var/lib/minikube/certs/`，並在完成前等待控制平面回報健康狀態。

- **containerd** — the container runtime that actually starts/stops the apiserver container, on kubelet's instruction. (We switched from the default `cri-o` to `containerd` mid-session because `cri-o` was crashing on this rootless-podman/macOS combo with an unrelated `rlimit` bug.)

  **containerd** — 依照 kubelet 的指示，實際負責啟動/停止 apiserver container 的 runtime。(這次過程中把預設的 `cri-o` 換成 `containerd`，是因為 `cri-o` 在這個 rootless-podman/macOS 組合下會撞到一個跟這次加密無關的 `rlimit` bug 而炸機。)

- **hostPath volume** — the Kubernetes volume type that mounts a directory/file from the node's own filesystem straight into a container. This is the *only* mechanism available to feed a static pod any file, since no ConfigMap/Secret machinery exists yet at that point in the boot sequence.

  **hostPath volume** — 一種 Kubernetes volume 類型，把 node 本機檔案系統裡的目錄/檔案直接掛進 container。這是餵資料給靜態 Pod 的**唯一**辦法，因為開機這個階段根本還沒有 ConfigMap/Secret 這種機制可用。

- **`/var/lib/minikube/certs/`** — the directory kubeadm itself uses to store TLS certs, and which is *already* declared as a hostPath mount on the apiserver static pod (that's how apiserver serves HTTPS at all). This turned out to be the key to the whole problem.

  **`/var/lib/minikube/certs/`** — kubeadm 自己拿來放 TLS 憑證的目錄，而且**本來就**被宣告成 apiserver 靜態 Pod 的 hostPath 掛載點(apiserver 就是靠這裡的檔案才能提供 HTTPS)。這個目錄後來成了解決整個問題的關鍵。

- **`encryption-config.yaml`** — the actual file kube-apiserver reads via `--encryption-provider-config`. Contains the encryption provider (`aescbc`) and a base64-encoded key.

  **`encryption-config.yaml`** — kube-apiserver 透過 `--encryption-provider-config` 這個參數實際讀取的檔案，裡面寫著加密演算法(`aescbc`)跟一組 base64 編碼的金鑰。

- **`KubeletInUserNamespace`** — a genuine, upstream Kubernetes feature gate (not Podman-specific, not minikube's invention) that lets kubelet itself run inside a Linux user namespace — i.e., without real root privileges. minikube automatically enables it whenever the podman driver runs in rootless mode. It's still alpha/experimental upstream.

  **`KubeletInUserNamespace`** — 一個貨真價實的 Kubernetes 官方 feature gate(不是為 Podman 設計、也不是 minikube 發明的)，讓 kubelet 本身能在 Linux user namespace 裡運作──也就是沒有真正 root 權限的情況下運作。只要 minikube 偵測到 podman driver 是用 rootless 模式，就會自動開啟這個功能。這個功能上游目前仍是 alpha/實驗階段。

- **kubectl** — the CLI client, talks to kube-apiserver's HTTP API once the cluster is actually alive. A different layer entirely from the node-level SSH/file work described above — kubectl only works *after* boot, SSH/file edits are how we intervened *during* boot.

  **kubectl** — 叢集指令列客戶端，在叢集真正活起來之後透過 apiserver 的 HTTP API 溝通。跟前面講的 node 層級 SSH/檔案操作完全是不同層次──kubectl 只能在開機**完成之後**用，SSH/檔案編輯則是我們在開機**過程中**介入的手段。

## What we originally tried to do — and why it kept failing

## 原本想怎麼做──以及為什麼一直失敗

The textbook approach (and what works on a normal Kubernetes cluster — EKS, GKE, or kubeadm on a real, non-rootless Linux box) is: create a brand-new directory (e.g. `/etc/kubernetes/encryption/`), put the config file there, and declare a *new* `volumeMounts`/`volumes` entry on the apiserver static pod pointing at it.

教科書式的標準做法(在一般的 Kubernetes 叢集上都行得通──EKS、GKE、或是在真實、非 rootless 的 Linux 機器上跑 kubeadm)是：新建一個全新目錄(例如 `/etc/kubernetes/encryption/`)，把設定檔放進去，然後在 apiserver 的靜態 Pod 上新增一組 `volumeMounts`/`volumes` 指向它。

We tried this three separate ways, and all three failed with the identical symptom — the apiserver container reported `open /etc/kubernetes/encryption/encryption-config.yaml: no such file or directory`, even when the file was verified present on the node and the volume mount was verified present in the manifest:

我們用三種不同方式試了這個做法，全部都出現一模一樣的失敗症狀──apiserver container 回報 `open /etc/kubernetes/encryption/encryption-config.yaml: no such file or directory`，即使檔案確實存在於 node 上、manifest 裡也確實宣告了對應的 volume mount：

1. **Hot-editing the running manifest** — SSH into the node, directly edit the live `kube-apiserver.yaml`, let kubelet restart the pod. Result: kubelet either silently kept running the *old* pod definition, or crash-looped on the new one.

   **熱編輯正在跑的 manifest** — SSH 進 node，直接改正在運作中的 `kube-apiserver.yaml`，讓 kubelet 重啟該 Pod。結果：kubelet 要不是悄悄繼續跑**舊的** Pod 定義，就是對新的定義陷入 crash loop。

2. **kubeadm-native `--extra-config`** — full cluster rebuild (`minikube delete` + `minikube start --extra-config=apiserver.encryption-provider-config=...`), so the flag is baked into kubeadm's *first-ever* rendering of the manifest, no hot-reload involved. Result: the flag landed correctly, but kubeadm's `--extra-config` mechanism only writes to `apiServer.extraArgs` (command-line flags) — it has no way to also declare the matching `extraVolumes` entry, so the container still couldn't see the file.

   **kubeadm 原生的 `--extra-config`** — 完整重建叢集(`minikube delete` + `minikube start --extra-config=apiserver.encryption-provider-config=...`)，讓這個參數在 kubeadm**第一次生成** manifest 時就內建進去，完全不牽涉熱重載。結果：參數確實成功寫進去了，但 kubeadm 的 `--extra-config` 機制只會寫進 `apiServer.extraArgs`(指令列參數)──它沒辦法同時宣告對應的 `extraVolumes`，所以 container 依然看不到檔案。

3. **Race-condition patch during boot** — start the cluster in the background, watch for the manifest file to appear on the node, and patch in the missing volume mount within seconds, before kubeadm's ~4-minute health-check timeout. Result: the volume mount was verified present in the final manifest kubelet actually used — and the container *still* failed with the same "no such file" error.

   **開機瞬間搶時間 patch** — 在背景啟動叢集，緊盯 node 上 manifest 檔案何時出現，在 kubeadm 大約 4 分鐘的健康檢查逾時之前，搶著把缺少的 volume mount 補進去。結果：確認 kubelet 最終使用的 manifest 裡，volume mount 確實存在──但 container **依然**出現一樣的「檔案不存在」錯誤。

At that point the working hypothesis was that `KubeletInUserNamespace`'s nested user-namespace mechanics were specifically breaking *newly declared* hostPath mounts, while mounts the base image/kubeadm already knew about from the start (certs, etc.) worked fine.

到這個階段的推測是：`KubeletInUserNamespace` 的巢狀 user namespace 機制，可能專門會破壞「新宣告」的 hostPath 掛載，而 base image/kubeadm 一開始就知道的掛載點(例如憑證目錄)則運作正常。

## The actual fix — and what it revealed

## 真正的解法──以及它揭露的真相

A web/GitHub search turned up [minikube issue #9339](https://github.com/kubernetes/minikube/issues/9339) and a blog post ([suraj.io](https://suraj.io/post/apiserver-in-minikube-static-configs/)) describing the *exact same* symptom — and both used a completely different fix: **don't add a new hostPath mount at all.** Instead, drop the custom config file inside `/var/lib/minikube/certs/`, the directory the apiserver static pod already mounts (that's how it reads its own TLS certs). Point `--encryption-provider-config` at a path inside that existing, already-working mount.

透過網路/GitHub 搜尋，找到 [minikube issue #9339](https://github.com/kubernetes/minikube/issues/9339) 跟一篇部落格文章([suraj.io](https://suraj.io/post/apiserver-in-minikube-static-configs/))，描述的是**一模一樣**的症狀──而兩者用的解法完全不同：**根本不要新增 hostPath 掛載點。** 而是把自訂設定檔直接放進 `/var/lib/minikube/certs/`──這個 apiserver 靜態 Pod 本來就會掛載的目錄(apiserver 就是靠這裡讀自己的 TLS 憑證)。讓 `--encryption-provider-config` 指向這個**已經存在、已經確定能運作**的掛載點裡的路徑。

Importantly, the blog post's example used the `kvm2` driver — not podman, not rootless. That's a strong signal this is a **general minikube limitation** (adding brand-new hostPath volumes to control-plane static pods is unreliable, full stop), not something specific to the rootless-podman/`KubeletInUserNamespace` setup we were debugging. The original hypothesis was never fully confirmed or disproven — it just turned out to not matter, because the actual, community-documented fix sidesteps the whole question.

值得注意的是，那篇部落格文章的範例用的是 `kvm2` driver──不是 podman，也不是 rootless。這是個很強的訊號，代表這其實是 **minikube 本身的通用限制**(幫控制平面的靜態 Pod 新增全新的 hostPath 掛載點本來就不可靠，就這麼簡單)，跟我們原本在除錯的 rootless-podman/`KubeletInUserNamespace` 設定沒有直接關係。原本的假設從頭到尾沒有被真正證實或推翻──只是後來發現它根本不重要，因為社群已經驗證過的解法，直接繞開了這整個問題。

Applying the fix: full `minikube delete` + fresh `minikube start --container-runtime=containerd --extra-config=apiserver.encryption-provider-config=/var/lib/minikube/certs/encryption-config.yaml`, racing to place the config file into `/var/lib/minikube/certs/` right after SSH became reachable (same timing pattern as before, but targeting the proven-working directory this time). It succeeded on the first try — no crash loop, no retries, `minikube start` reported `Done!` cleanly.

實際套用解法：完整 `minikube delete` + 全新 `minikube start --container-runtime=containerd --extra-config=apiserver.encryption-provider-config=/var/lib/minikube/certs/encryption-config.yaml`，在 SSH 一連得上就搶時間把設定檔放進 `/var/lib/minikube/certs/`(跟之前一樣的搶時間手法，只是這次目標換成已經證實可行的目錄)。第一次嘗試就成功──沒有 crash loop、沒有重試，`minikube start` 乾淨俐落地回報 `Done!`。

## Verifying it actually worked

## 驗證是否真的生效

Trust but verify: created a Secret with a unique random marker string, then directly grepped etcd's raw on-disk data file (`/var/lib/minikube/etcd/member/snap/db`) for that marker.

信任但要驗證：建立一個帶有隨機亂數字串的 Secret，然後直接對 etcd 底層原始資料檔(`/var/lib/minikube/etcd/member/snap/db`)搜尋這個字串。

- The plaintext marker string: **0 matches** — not visible anywhere in the raw store.

  明文亂數字串：**0 筆符合** ── 在原始儲存檔裡完全找不到明文。

- The `k8s:enc:aescbc:` prefix (which Kubernetes prepends to ciphertext when this provider is active): **present** — proving AES-CBC encryption is genuinely active, not just the flag being accepted with no real effect.

  `k8s:enc:aescbc:` 前綴(這個加密演算法生效時，Kubernetes 會自動加在密文前面)：**確實存在** ── 證明 AES-CBC 加密是真的在運作，不只是參數被接受但沒有實際效果。

- Reading the Secret back through the normal `kubectl get secret ... | base64 -d` path: still returns the correct plaintext value — decryption on read works transparently, exactly as intended.

  透過一般的 `kubectl get secret ... | base64 -d` 讀取：依然正確讀回原本的明文值──讀取時的解密完全透明，跟預期一致。

## What's now checked into the repo

## 現在已經寫進專案裡的東西

`deploy/setup-minikube.sh` automates the whole recipe (background start, race to place the config, apply the shared Postgres) so future `minikube delete` + rebuilds don't silently lose encryption-at-rest by reverting to a plain `minikube start`. `CLAUDE.md` and `docs/prod-provisioning.md` were updated to point at this script instead of the bare `minikube start` command.

`deploy/setup-minikube.sh` 把整套流程自動化了(背景啟動、搶時間放設定檔、部署共用的 Postgres)，這樣以後 `minikube delete` 重建時，就不會因為誤用單純的 `minikube start` 而悄悄失去加密功能。`CLAUDE.md` 跟 `docs/prod-provisioning.md` 也都更新成指向這支腳本，而不是裸的 `minikube start` 指令。
