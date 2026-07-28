# How Prompt Caching Works in Snoopy

# Snoopy 的 Prompt Caching 是怎麼運作的

The bot makes exactly **one** Gemini call per message, and the system prompt (persona + rules + 15 tool schemas) is long and almost never changes. Re-sending that static prefix on every request would be paying for the same tokens over and over. So we cache it **server-side on Gemini** (`client.caches.create`) and reuse it for every request. The core lives in `core/gemini_client.py`.

機器人每則訊息只呼叫 Gemini **一次**，而系統提示（人設 + 規則 + 15 個工具 schema）又長又幾乎不變。每個請求都重送這段靜態前綴，等於一直為同樣的 token 付費。所以我們把它快取在 **Gemini 伺服器端**（`client.caches.create`），之後每個請求都重用同一份。核心程式碼在 `core/gemini_client.py`。

## What is cached vs. what is not

## 什麼有快取、什麼沒有

This split is the whole design. It is cut deliberately so the cache stays static and shareable.

這個切分就是整個設計的重點。刻意這樣切，是為了讓快取維持「靜態、可共用」。

| Cached (static, rarely changes) 有快取（靜態、極少變） | **Not** cached (dynamic, per-request) **沒有**快取（動態、每請求） |
|---|---|
| Bot persona, capability descriptions, formatting rules. 人設、能力說明、格式規則。 | Current date/time — stamped onto the last user message (`_stamp_date`). 目前日期時間 → 蓋在最後一則 user message 上。 |
| The 15 tool declarations (function-calling schemas). 15 個工具宣告（function-calling schema）。 | Conversation history — passed as `contents` on every call. 對話歷史 → 每次以 `contents` 傳入。 |
| | **Per-household data (member roster / chore schedule)** — rides as a per-request context message (`core/household.py`). **每戶的家庭資料（成員名冊 / 家務表）** → 以每請求的 context message 帶入。 |

Note: the module docstring at the top of `core/gemini_client.py` historically claimed the roster and chore schedule were cached. That is stale — the authoritative behavior is the code and the comment near `_ensure_cache`: the cache holds **only** static content, and household data travels as a per-request message.

備註：`core/gemini_client.py` 最上方的 module docstring 以前寫「名冊 + 家務表有被快取」，那是過時的敘述——真正的行為以程式碼與 `_ensure_cache` 附近的註解為準：快取**只**放靜態內容，家庭資料改走每請求的 context message。

## Why household data is kept out of the cache

## 為什麼家庭資料不放進快取

Gemini caches have a **4096-token minimum**. If we made "one cache per guild," that floor would be multiplied by the number of guilds — a lot of wasted paid tokens. Instead the cache is a **single, purely static blob shared across all guilds**, and each household's data is injected as one small context message per request. Multi-tenancy stays cheap.

Gemini 的快取有 **4096 token 下限**。如果做成「每個 guild 一份快取」，這個下限會被 guild 數量乘上去——大量浪費的付費 token。所以快取做成**跨所有 guild 共用的一份純靜態內容**，每戶的資料則以一則小小的 context message 在每次請求時注入。這樣多租戶下成本才不會爆。

## Cache lifecycle

## 快取生命週期

The relevant pieces are `_CacheHandle` and `_ensure_cache()`.

相關的部分是 `_CacheHandle` 與 `_ensure_cache()`。

- **One cache per model tier.** LOW / MEDIUM / HIGH may target different models, so each gets its own handle in `self._caches`.
- **一個 model tier 一份快取。** LOW / MEDIUM / HIGH 可能指向不同 model，所以各自在 `self._caches` 裡有自己的 handle。

- **TTL is configurable** via `CACHE_TTL_SECONDS`; the cache is recreated when it expires.
- **TTL 可設定**（`CACHE_TTL_SECONDS`）；過期時會重建。

- **Expiry guard.** A handle is treated as invalid **90 seconds before** its real expiry, so we never send an already-expired cache name and trigger an error.
- **到期保護。** handle 在真正到期前 **90 秒**就先視為失效，這樣就不會送出已過期的 cache name 而觸發錯誤。

- **Metrics.** Every outcome is counted: `cache_events_total{event="created"|"create_failed"|"hit"|"uncached"}` — visible on the Grafana dashboard.
- **指標。** 每種結果都會計數：`cache_events_total{event="created"|"create_failed"|"hit"|"uncached"}`——可在 Grafana dashboard 上看到。

## The one hard constraint

## 一個硬性約束

A request that uses `cached_content` **must not also pass `tools` or `system_instruction`** — the tool declarations are baked **into** the cache. So there are two branches:

用了 `cached_content` 的請求**不能同時再傳 `tools` 或 `system_instruction`**——工具宣告已經**烤進**快取裡了。所以有兩條分支：

- **Cache hit:** the `config` carries only `cached_content`.
- **命中快取：** `config` 只帶 `cached_content`。

- **Uncached fallback:** `system_instruction` + `tools` are passed inline instead.
- **未快取的 fallback：** 改為 inline 傳入 `system_instruction` + `tools`。

## Fault tolerance and prerequisites

## 容錯與前提

- If cache creation fails — content below the 4096-token minimum, or the free tier returning `limit: 0` — generation **falls back transparently** to uncached mode. The bot keeps working; it just pays full price for that request.
- 如果建立快取失敗——內容不到 4096 token 下限，或免費方案回 `limit: 0`——生成會**透明降級**成未快取模式。機器人照常運作，只是那個請求付全額。

- **A paid Gemini tier is required.** The free tier's context-cache quota is 0.
- **需要 Gemini 付費方案。** 免費 tier 的 context cache 額度是 0。

## The key code path

## 關鍵程式碼路徑

```python
# _ensure_cache(model): build the cache only if the handle is stale
cache_kwargs = {
    "display_name": "snoopy_" + model.replace("/", "_").replace("-", "_"),
    "system_instruction": system_prompt,   # static persona + rules
    "ttl": str(settings.cache_ttl_seconds) + "s",
}
if settings.action_protocol == "tools":
    cache_kwargs["tools"] = [tool_registry.as_tool()]   # 15 tool schemas baked in

# at generation time:
if use_cache:
    await self._ensure_cache(model)
    handle = self._caches.get(model)
    if handle and handle.valid():
        gen_kwargs["cached_content"] = handle.name   # hit: tools/system live in the cache
# else: pass system_instruction + tools inline (uncached fallback)
```

## Takeaways

## 重點回顧

1. Cache **only the truly static prefix**; keep anything per-tenant or time-varying out of it.
1. 只快取**真正靜態的前綴**；任何跟租戶或時間有關的東西都留在快取外。

2. **Share one cache across tenants** to avoid multiplying the 4096-token floor.
2. **跨租戶共用一份快取**，避免把 4096 token 下限乘上去。

3. **Never let the cache become a single point of failure** — always keep a transparent uncached fallback.
3. **絕不讓快取變成單點故障**——永遠保留一條透明的未快取 fallback。
