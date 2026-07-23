# Prod deploy log — snoopy on k3s (2026-07-22)

*正式環境部署紀錄 — snoopy 遷移到 k3s(2026-07-22)*

This is a record of the day snoopy's production was cut over from the old
single-VM **Podman/Quadlet + SQLite** setup to **single-node k3s on a fresh OCI
Ampere A1.Flex node** with PostgreSQL. It follows the gate structure in
[prod-k3s-runbook.md](prod-k3s-runbook.md) and captures the real problems hit
along the way and how each was fixed.

*本文記錄 snoopy 正式環境從舊的單一 VM(Podman/Quadlet + SQLite)切換到「單節點 k3s + PostgreSQL」的過程,執行環境是一台全新的 OCI Ampere A1.Flex 節點。流程對應 [prod-k3s-runbook.md](prod-k3s-runbook.md) 的各個 Gate,並記錄過程中實際踩到的問題與各自的修法。*

---

## Outcome

*結果*

Prod is **live** on k3s. All gates 0–8 completed: the instance was provisioned,
k3s installed, shared Postgres stood up in the `data` namespace, data migrated
from SQLite (row counts matched), the bot deployed as a single-replica
`Recreate` Deployment, both the manual deploy and the automated CI tag path
(`v1.0.0`) succeeded, and the old box was decommissioned (Gate 8) — the old
instance is terminated.

*正式環境已在 k3s 上線。Gate 0~8 全部完成:開好機器、裝好 k3s、在 `data` namespace 架好共用 Postgres、把資料從 SQLite 遷移過來(行數一致)、以單一副本 `Recreate` 策略部署 bot,手動部署與自動化 CI tag 路徑(`v1.0.0`)都成功,舊機器也已下線(Gate 8)—— 舊 instance 已終止。*

---

## Environment

*環境*

The new node is an OCI `VM.Standard.A1.Flex` (2 OCPU / 12 GB / 200 GB, Oracle
Linux 9, ARM64), running single-node k3s with `--disable traefik --disable
servicelb`. The bot needs no inbound ports (Discord + Gemini are outbound), so
only SSH (22) is open. Images are built on the node with Podman and imported
straight into k3s's containerd — there is no registry.

*新節點是 OCI `VM.Standard.A1.Flex`(2 OCPU / 12 GB / 200 GB,Oracle Linux 9,ARM64),跑單節點 k3s,啟動時關掉 traefik 與 servicelb。bot 不需要任何對內連入的埠(Discord 與 Gemini 都是對外連線),所以只開放 SSH(22)。映像檔在節點上用 Podman 建置後,直接匯入 k3s 的 containerd —— 沒有使用任何 registry。*

The shared Postgres 17 lives in a neutral `data` namespace
(`postgres.data.svc:5432`), with a per-app database `snoopy_home` owned by a
least-privilege login role `snoopy_rw`. This is the same shape staging uses on
minikube, and leaves room for transigen/gelp to share the instance later.

*共用的 Postgres 17 放在中性的 `data` namespace(`postgres.data.svc:5432`),各 app 一個獨立資料庫;snoopy 的資料庫 `snoopy_home` 由最小權限登入角色 `snoopy_rw` 擁有。這與 staging 在 minikube 上的架構一致,也預留了日後 transigen/gelp 共用同一個 Postgres 的空間。*

---

## The safety rule that shaped the ordering

*決定執行順序的安全準則*

Never run two bots on one Discord token — two instances answer every message
twice and fire every reminder twice. This is why the Deployment uses `replicas:
1` with `strategy: Recreate` (never two pods overlapping), and why the cutover
order is strict: **stop the old bot (Gate 4) → migrate data (Gate 5) → start the
new pod (Gate 6)**, so the bot never races the migrator on the same rows and two
bots never run at once.

*同一個 Discord token 絕不能同時跑兩個 bot —— 兩個實例會把每則訊息回覆兩次、每個提醒觸發兩次。這就是為什麼 Deployment 用 `replicas: 1` 搭配 `strategy: Recreate`(兩個 pod 永不重疊),也是為什麼切換順序很嚴格:**先停舊 bot(Gate 4)→ 遷移資料(Gate 5)→ 啟動新 pod(Gate 6)**,確保 bot 不會和遷移程式搶同一批資料列,也確保兩個 bot 不會同時存在。*

---

## Issues hit and how they were fixed

*過程中踩到的問題與修法*

These are the real snags encountered during the cutover. Each fix was folded
back into the runbook / CI so the next deploy won't rediscover them.

*以下是切換過程中實際遇到的坑。每個修法都已回寫進 runbook / CI,讓下次部署不會再重新踩一次。*

### 1. SELinux blocked the migration bind mount

*SELinux 擋住了遷移用的 bind mount*

Running `python -m storage.migrate` inside the built image with `-v
~/snoopy_home:/app` failed with `ModuleNotFoundError: No module named
'storage'`. On Oracle Linux 9 (SELinux enforcing), an unlabeled podman bind
mount is blocked, so the container saw an empty `/app`. Fix: add `:z` to the
mount (`-v ~/snoopy_home:/app:z`) so podman relabels the directory for the
container. The SQLite file has to come through this mount because
`.dockerignore` excludes `*.db`, so it is not baked into the image.

*在建好的映像裡用 `-v ~/snoopy_home:/app` 跑 `python -m storage.migrate` 時,報 `ModuleNotFoundError: No module named 'storage'`。在 Oracle Linux 9(SELinux enforcing)上,未加標籤的 podman bind mount 會被擋掉,容器看到的是空的 `/app`。修法:在掛載後面加 `:z`(`-v ~/snoopy_home:/app:z`),讓 podman 為容器重新標記該目錄。SQLite 檔必須透過這個掛載進來,因為 `.dockerignore` 排除了 `*.db`,它並沒有被打包進映像裡。*

### 2. `Settings()` required Discord/Gemini keys the migration never uses

*`Settings()` 要求了遷移根本用不到的 Discord/Gemini 金鑰*

`storage.migrate` imports `config`, whose pydantic `Settings()` requires
`discord_token` and `gemini_api_key` at construction — even though the schema
migration only uses `DATABASE_URL`. Fix: pass throwaway values (`-e
DISCORD_TOKEN=x -e GEMINI_API_KEY=x`) on the schema step. Nothing connects to
Discord or Gemini; the fields just have to exist for pydantic to build the
global `settings`. The data-copy step doesn't import `config`, so it needs no
stubs.

*`storage.migrate` 會 import `config`,而其中的 pydantic `Settings()` 在建構時要求 `discord_token` 與 `gemini_api_key` —— 即使 schema 遷移只用到 `DATABASE_URL`。修法:在 schema 步驟傳入隨便的暫代值(`-e DISCORD_TOKEN=x -e GEMINI_API_KEY=x`)。這一步不會連 Discord 或 Gemini,這兩個欄位只是為了讓 pydantic 能建出全域的 `settings`。而資料複製那一步沒有 import `config`,所以不需要暫代值。*

### 3. `sudo k3s` — command not found

*`sudo k3s` —— 找不到指令*

`podman save … | sudo k3s ctr images import -` failed with `sudo: k3s: command
not found`, even though `kubectl` worked. The k3s binary is at
`/usr/local/bin/k3s`, but `sudo`'s `secure_path` doesn't include
`/usr/local/bin`. Fix: use the full path — `sudo /usr/local/bin/k3s ctr images
import -` — which also matches the `NOPASSWD` sudoers rule exactly, so it stays
passwordless. This was fixed in both the runbook and the CI workflow.

*`podman save … | sudo k3s ctr images import -` 報 `sudo: k3s: command not found`,但 `kubectl` 卻能用。k3s 執行檔在 `/usr/local/bin/k3s`,但 `sudo` 的 `secure_path` 不包含 `/usr/local/bin`。修法:用完整路徑 —— `sudo /usr/local/bin/k3s ctr images import -` —— 這也剛好完全符合 `NOPASSWD` sudoers 規則,所以維持免密碼。runbook 與 CI workflow 都已修正。*

### 4. `deploy/k8s/` didn't exist on the node

*節點上沒有 `deploy/k8s/`*

`kubectl apply -k deploy/k8s/overlays/prod` failed with "not a valid
directory". The k8s manifests were committed only on the `deploy/prod-k3s`
branch, which had never been pushed — and the node was `git clone`d on `main`,
which lacked them. Fix: push the branch, open a PR, merge it into `main`, then
check out `main` on the node. This is also why the eventual clean state has the
manifests on `main` (a fresh node rebuild clones `main`).

*`kubectl apply -k deploy/k8s/overlays/prod` 報「not a valid directory」。k8s manifest 只 commit 在 `deploy/prod-k3s` 分支上,而該分支從未 push —— 節點是用 `main` 分支 `git clone` 的,上面沒有這些檔案。修法:push 分支、開 PR、合併進 `main`,再在節點上 checkout `main`。這也是為什麼最終乾淨狀態要讓 manifest 進到 `main`(重建全新節點時會 clone `main`)。*

### 5. CI SSH handshake failed — deploy key not authorized on the new node

*CI 的 SSH 交握失敗 —— 新節點沒授權 deploy 金鑰*

The `v1.0.0` tag triggered CI, which failed with `ssh: handshake failed …
unable to authenticate [none publickey]`. The node's `sshd` was fine (manual SSH
worked), but CI authenticates with the `OCI_SSH_PRIVATE_KEY` secret, whose
public half was never added to the new node's `authorized_keys`. The fresh
instance was provisioned with a different keypair than the old deploy key. Fix:
add the deploy key's public half to `opc`'s `authorized_keys` on the node, then
re-run the failed `deploy` job (no re-tag needed). CI then went green.

*`v1.0.0` tag 觸發了 CI,結果報 `ssh: handshake failed … unable to authenticate [none publickey]`。節點的 `sshd` 沒問題(手動 SSH 可通),但 CI 是用 `OCI_SSH_PRIVATE_KEY` secret 認證,而它的公鑰從未被加進新節點的 `authorized_keys`。這台全新機器開機時用的金鑰對,跟舊的 deploy 金鑰不同。修法:把 deploy 金鑰的公鑰加進節點上 `opc` 的 `authorized_keys`,再重跑失敗的 `deploy` job(不需重新打 tag)。之後 CI 就綠了。*

### 6. Google Calendar creds missing in prod (found in the live smoke test)

*正式環境缺少 Google Calendar 憑證(實機 smoke test 時發現)*

During the live Discord smoke test, creating a calendar event failed with
`service_build_failed … No such file or directory:
'api-project-…json'`. Root cause: the `snoopy-secrets` had
`GOOGLE_SERVICE_ACCOUNT_JSON` set to a **bare filename from the dev machine**,
but that file isn't in the pod (`.dockerignore` excludes `*.json`, so it's never
baked into the image). The intended path is `GOOGLE_SA_JSON_B64` — `entrypoint.sh`
base64-decodes it to `/app/service_account.json` and re-exports
`GOOGLE_SERVICE_ACCOUNT_JSON` to that path. Fix: `scp` the SA JSON to the node,
`base64 -w0` it, `kubectl patch` it into the secret as `GOOGLE_SA_JSON_B64`,
remove the stale bare-path key, and `rollout restart`.

*在 Discord 實機 smoke test 時,建立日曆事件失敗,報 `service_build_failed … No such file or directory: 'api-project-…json'`。根因:`snoopy-secrets` 裡的 `GOOGLE_SERVICE_ACCOUNT_JSON` 被填成「開發機上的裸檔名」,但那個檔案不在 pod 裡(`.dockerignore` 排除 `*.json`,不會被打包進映像)。正確做法是用 `GOOGLE_SA_JSON_B64` —— `entrypoint.sh` 會把它 base64 解碼成 `/app/service_account.json` 並把 `GOOGLE_SERVICE_ACCOUNT_JSON` 指過去。修法:把 SA JSON `scp` 到節點、`base64 -w0` 編碼、用 `kubectl patch` 以 `GOOGLE_SA_JSON_B64` 寫進 secret、移除舊的裸路徑 key,再 `rollout restart`。*

A verification red herring cost extra time: `kubectl exec … echo
$GOOGLE_SERVICE_ACCOUNT_JSON` kept returning blank even after the fix.
`kubectl exec` starts a fresh shell that inherits the pod's **declared** env
(`envFrom`), not the **runtime `export`** that `entrypoint.sh` set on PID 1
before `exec python main.py`. So the exec shell never sees the exported path even
though the real bot process has it. Verify the right way instead: `ls -l
/app/service_account.json` for the decoded file, and `cat /proc/1/environ | tr
'\0' '\n' | grep -i google` to read PID 1's actual environment.

*一個驗證上的假線索多花了時間:修好之後 `kubectl exec … echo $GOOGLE_SERVICE_ACCOUNT_JSON` 一直回傳空白。`kubectl exec` 開的是新 shell,拿到的是 pod 宣告的環境變數(`envFrom`),而不是 `entrypoint.sh` 在 `exec python main.py` 之前於 PID 1 上做的執行期 `export`。所以 exec shell 看不到那個變數,但真正的 bot 程序其實有。正確驗證方式:用 `ls -l /app/service_account.json` 看解碼出來的檔案,以及 `cat /proc/1/environ | tr '\0' '\n' | grep -i google` 讀 PID 1 的實際環境變數。*

Security note: while debugging, the service-account **private key was pasted in
plaintext** into a terminal/chat, so it must be treated as compromised — rotate
it (GCP Console → IAM → Service Accounts → Keys: delete the exposed key, create a
new one) and re-run the b64 step with the new JSON. Also, the target calendar
must be shared with the SA's `client_email` for events to actually write.

*安全提醒:除錯過程中 service-account 的**私鑰以明文貼進了終端機/對話**,必須當作已外洩處理 —— 請輪替金鑰(GCP Console → IAM → 服務帳戶 → 金鑰:刪掉外洩的那把、產一把新的),再用新的 JSON 重做 b64 步驟。另外,目標日曆必須分享給該 SA 的 `client_email`,事件才寫得進去。*

---

## Deep dive: why the env var only lives in PID 1

*深入:為什麼那個環境變數只存在於 PID 1*

The verification confusion in issue 6 is worth understanding properly, because it
is a property of how containers and environment variables work, not a one-off
quirk. The root fact: **environment variables are per-process, copied from parent
to child only at fork/exec time.** There is no container-wide shared environment
table — each process carries its own copy, and a later change in one process does
not propagate to processes already running.

*問題 6 那個驗證上的混淆值得徹底搞懂,因為它是「容器 + 環境變數」運作方式的本質,不是一次性的怪現象。根本事實是:**環境變數是每個 process 各自一份,只在 fork/exec 那一刻從父程序複製給子程序。** 容器裡沒有一張「全容器共用的環境變數表」—— 每個 process 帶著自己的副本,某個 process 之後的修改不會傳播給已經在跑的其他 process。*

At container start, the runtime (containerd) builds PID 1's initial environment
from the **pod spec** (`env` / `envFrom`). Here PID 1 is `entrypoint.sh`, which at
runtime base64-decodes the secret into `/app/service_account.json` and runs
`export GOOGLE_SERVICE_ACCOUNT_JSON=/app/service_account.json`. That `export`
mutates **only PID 1's own in-memory environment** — it is never written back to
the pod spec. Then `exec python main.py` replaces the shell with python **in the
same process**, preserving the environment, so python (now PID 1) inherits the
exported path.

*容器啟動時,runtime(containerd)依據 **pod spec**(`env` / `envFrom`)建立 PID 1 的初始環境。這裡 PID 1 是 `entrypoint.sh`,它在執行期把 secret base64 解碼成 `/app/service_account.json`,並執行 `export GOOGLE_SERVICE_ACCOUNT_JSON=/app/service_account.json`。這個 `export` **只改到 PID 1 自己記憶體裡的環境**,從來沒有寫回 pod spec。接著 `exec python main.py` 在**同一個 process** 裡把 shell 換成 python,環境整份保留,所以 python(現在是 PID 1)繼承了那個路徑。*

```
pod spec env/envFrom ──▶ PID 1 (entrypoint.sh)  ──export──▶ PID 1 mem env ──exec──▶ python (still PID 1) ✅ has it
                                                                                          ▲
kubectl exec ─────────▶ containerd spawns a BRAND-NEW process from the pod spec ─────────┘ ❌ never copies PID 1's mutated env
```

`kubectl exec` is **not** a child of PID 1. containerd spawns a brand-new process
and builds *its* environment from the pod spec again — it does **not** copy PID
1's mutated, runtime-exported environment. So the exec shell is structurally blind
to anything `entrypoint.sh` exported at runtime. To see what the bot actually has,
read PID 1's real environment directly: `cat /proc/1/environ | tr '\0' '\n'` (the
kernel's record of the environment PID 1 was started with).

*`kubectl exec` **不是** PID 1 的子程序。containerd 會另外開一個全新的 process,再次依 pod spec 建立**它自己**的環境 —— 它**不會**複製 PID 1 那份被執行期改過的環境。所以 exec shell 在結構上就看不到 `entrypoint.sh` 在執行期 export 的任何東西。要看 bot 實際擁有什麼,直接讀 PID 1 的真實環境:`cat /proc/1/environ | tr '\0' '\n'`(核心記錄的、PID 1 啟動時的環境)。*

Why does this architecture force the PID-1 lookup at all? Because the value is
**generated at runtime**: the credential is a base64 JSON in a Secret, and the
file it points to (`/app/service_account.json`) does not exist until entrypoint
writes it — so the path can only be exported at runtime, never declared in the pod
spec. Value born at runtime ⇒ it lives only in PID 1 ⇒ verification must look at
PID 1.

*為什麼這套架構會逼你非得往 PID 1 找?因為那個值是**執行期才生出來的**:憑證是 Secret 裡一份 base64 JSON,而它指向的檔案(`/app/service_account.json`)在 entrypoint 寫出來之前根本不存在 —— 所以路徑只能在執行期 export,無法預先宣告在 pod spec 裡。值在執行期誕生 ⇒ 它只活在 PID 1 ⇒ 驗證就得看 PID 1。*

The more Kubernetes-native alternative avoids this entirely: mount the credential
as a **Secret volume** (kubelet projects it as a file before the container starts)
and declare `GOOGLE_SERVICE_ACCOUNT_JSON=/var/secrets/service_account.json`
directly in `env`. Then the path is in the pod spec, `kubectl exec` sees it too,
and there is no runtime decode step. The current entrypoint + `GOOGLE_SA_JSON_B64`
approach trades that transparency for a simpler manifest (the decode logic hides
inside the image); the cost is exactly this "the var only lives in PID 1" gotcha.

*更「Kubernetes 原生」的替代做法可以完全避開這件事:把憑證用 **Secret volume** 掛載(kubelet 會在容器啟動前把它投影成檔案),並直接在 `env` 裡宣告 `GOOGLE_SERVICE_ACCOUNT_JSON=/var/secrets/service_account.json`。這樣路徑就在 pod spec 裡、`kubectl exec` 也看得到,而且沒有執行期解碼步驟。目前的 entrypoint + `GOOGLE_SA_JSON_B64` 做法,是用「manifest 較簡單(解碼邏輯藏在映像裡)」換掉那份透明度;代價正是這個「變數只活在 PID 1」的坑。*

---

## Will a future deploy hit this credential problem again?

*之後的部署還會再遇到這個憑證問題嗎?*

**Routine code deploys (cut a new `v*` tag) — no.** The `snoopy-secrets` Secret is
created **once, out-of-band** in Gate 3 (`kubectl create secret
--from-env-file`). It is **not** in the kustomize manifests — the Deployment only
*references* it via `secretRef` (`envFrom`), it doesn't define it — and the CI
deploy job never touches Secrets (it only does `checkout → build → import → apply
-k → set image → rollout`). `kubectl apply -k` doesn't manage the Secret either,
so the `GOOGLE_SA_JSON_B64` you patched in **persists across every tag deploy**.
Fixed once, stays fixed.

*日常 code 部署(打新的 `v*` tag)—— 不會。`snoopy-secrets` 這個 Secret 是在 Gate 3 **一次性、獨立於 manifest 之外**建立的(`kubectl create secret --from-env-file`)。它**不在** kustomize manifest 裡 —— Deployment 只透過 `secretRef`(`envFrom`)*引用*它,並沒有定義它 —— 而 CI 部署 job 從不碰 Secret(只做 `checkout → build → import → apply -k → set image → rollout`)。`kubectl apply -k` 也不管理這個 Secret,所以你 patch 進去的 `GOOGLE_SA_JSON_B64` **會跨每一次 tag 部署持續存在**。修一次就一直有效。*

**Fresh node rebuild (re-run Gates 0–6) — only if `env.prod` is filled wrong
again.** That step is hand-typed. The trap is setting
`GOOGLE_SERVICE_ACCOUNT_JSON=<a path>` (e.g. copied from a dev `.env`) instead of
`GOOGLE_SA_JSON_B64=<base64 content>`. To stop this recurring, Gate 3 in
`docs/prod-k3s-runbook.md` was hardened with an explicit warning and the correct
`base64 -w0` generation command, so a rebuild that follows the runbook can't make
the same mistake.

*重建全新節點(重跑 Gate 0~6)—— 只有在「又把 `env.prod` 填錯」時才會。那一步是純手打的。陷阱就是填成 `GOOGLE_SERVICE_ACCOUNT_JSON=<路徑>`(例如從開發機的 `.env` 複製過來),而不是 `GOOGLE_SA_JSON_B64=<base64 內容>`。為了避免重蹈覆轍,`docs/prod-k3s-runbook.md` 的 Gate 3 已經加上明確警告與正確的 `base64 -w0` 產生指令,照著 runbook 重建就不會再犯同樣的錯。*

**Key rotation is the same flow.** Whenever the SA key is rotated, the new JSON
must be re-encoded (`base64 -w0`), patched into the Secret as `GOOGLE_SA_JSON_B64`,
and the deployment `rollout restart`ed — identical to the fix in issue 6, not a
deploy concern per se but easy to forget.

*輪替金鑰是同一套流程。每次換 SA 金鑰,都要把新的 JSON 重新編碼(`base64 -w0`)、以 `GOOGLE_SA_JSON_B64` patch 進 Secret,再 `rollout restart` —— 跟問題 6 的修法一模一樣,雖然不算部署本身的問題,但很容易忘。*

---

## Deploy model (how prod updates from here)

*部署模型(之後 prod 如何更新)*

CI (`.github/workflows/deploy.yml`) triggers only on `v*` tags, not on `main`
pushes. On a tag it runs tests, then SSHes to the node, checks out the tagged
commit, builds the image, imports it into containerd, `kubectl apply -k`s the
prod overlay, `set image`s to the tagged SHA, and waits on `rollout status`.
Because the Deployment is `Recreate` with a single replica, each deploy takes
the bot down briefly (~10–30s) then back up — never two bots at once.

*CI(`.github/workflows/deploy.yml`)只在 `v*` tag 時觸發,`main` 的 push 不會觸發。打 tag 後,它會跑測試,接著 SSH 進節點、checkout 該 tag 的 commit、建映像、匯入 containerd、對 prod overlay 執行 `kubectl apply -k`、`set image` 到該 tag 的 SHA,再等 `rollout status`。由於 Deployment 是單副本 `Recreate`,每次部署會讓 bot 短暫離線(約 10~30 秒)再回來 —— 絕不會同時存在兩個 bot。*

Image tags are the git commit short SHA (not `latest`), so every image maps
one-to-one to an exact code version, which makes rollback (`kubectl rollout
undo`) and debugging straightforward.

*映像 tag 用 git commit 的短 SHA(不是 `latest`),所以每個映像都一對一對應到某個確切的程式碼版本,讓 rollback(`kubectl rollout undo`)與除錯都很直接。*

---

## Verifying prod

*驗證正式環境*

Infrastructure health: confirm the running image is the CI-built SHA
(`kubectl -n snoopy get deploy snoopy -o jsonpath='{…image}'`), the pod is
`1/1 Running`, the logs show `migrations_done newly_applied=0` + Discord
connected, and `/ready` returns `{"discord": true, "database": true,
"scheduler": true}`.

*基礎設施健康度:確認正在跑的映像是 CI 建置的那個 SHA(`kubectl -n snoopy get deploy snoopy -o jsonpath='{…image}'`)、pod 是 `1/1 Running`、日誌顯示 `migrations_done newly_applied=0` 且 Discord 已連線,以及 `/ready` 回傳 `{"discord": true, "database": true, "scheduler": true}`。*

Functional end-to-end: talk to the real bot in Discord — mention it and confirm
a reply, set a reminder and confirm it fires (validates the scheduler), and ask
a tools query like "what chores do we have?" (validates function calling + DB
reads against the migrated data).

*功能端到端:直接在 Discord 上跟真實 bot 互動 —— @提及它並確認會回覆、設一個提醒並確認會觸發(驗證排程器),再問一個工具類問題例如「我們有哪些家務?」(驗證 function calling 與對遷移後資料的 DB 讀取)。*

---

## Pending

*待辦*

Gate 8 — decommission the old box — is **done**: the old instance has been
terminated. Note the rollback consequence: the "restart the old Quadlet bot" path
no longer exists. Data rollback still stands, though — the migrated
`snoopy_home.db` copy pulled to the new node in Gate 4 remains on disk there as the
data-recovery artifact; keep it until you're fully confident in prod.

*Gate 8 —— 下線舊機器 —— **已完成**:舊 instance 已終止。要注意 rollback 的影響:「重啟舊 Quadlet bot」這條路已經不存在了。不過資料層的 rollback 仍在 —— Gate 4 時抓到新節點上的 `snoopy_home.db` 副本還留在那台磁碟上,作為資料復原的備援;在你對正式環境完全放心之前先留著它。*

Also outstanding: the live Discord smoke test above, if not yet run against the
CI-deployed pod.

*另外尚待完成:若還沒對 CI 部署的 pod 跑過,上面提到的 Discord 實機 smoke test 也要補做。*

Security follow-up (from issue 6): **rotate the exposed Google service-account
key** — delete key `4c0a8ec79a13…` in GCP, create a new one, and re-run the
`GOOGLE_SA_JSON_B64` step with the new JSON.

*安全後續(來自問題 6):**輪替已外洩的 Google service-account 金鑰** —— 在 GCP 刪掉 `4c0a8ec79a13…` 這把、產一把新的,再用新的 JSON 重做一次 `GOOGLE_SA_JSON_B64` 步驟。*
