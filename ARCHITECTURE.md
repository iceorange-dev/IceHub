# IceHub 架構

本文件描述重構後的模組邊界與依賴規則。目標是讓每項功能可獨立測試、讓所有可寫入或可執行的操作有
明確安全邊界，並避免 `app/ost_app.py`、`backend/ost_backend.py` 再次成為巨型實作檔。

## Composition roots

IceHub 有四層 composition root，各自只負責組裝與生命週期，不承載 domain 細節：

1. `main.py`：最外層 process entry；安裝未捕捉例外 hooks，將退出狀態正規化。
2. `app/bootstrap.py`：建立 `QApplication`、套用 theme、取得單一實例 lock，最後啟動視窗。
3. `app/shell/main_window.py`：建立 `AppServices`、六個 pages 與 navigation，關閉時協作取消 workers。
4. `backend/facade.py`：`OSTBackend` 的 application facade；組合 catalog、library sources、Lua library 與
   tool updates，並管理 loop-local／注入的 HTTP client。

```text
process entry
  → Qt bootstrap
    → MainWindow + AppServices
      → pages / widgets / workers
        → OSTBackend
          → catalog | library | tool updates | installers
            → runtime | security | storage | deployment | HTTP/platform
```

除了這些 composition roots，不應在 import 時建立 thread、發出網路請求、建立資料目錄或執行安裝。

## Runtime 與 UI services

### `BackendRuntime`

`backend/runtime.py` 集中 backend 的環境相依項目：

- `AppPaths` 與 resource、config、cache、temp、tools、state 等衍生路徑；
- config、名稱、圖片、Sudama 與 Steamless manifest 的 `AtomicJsonStore`；
- process 共用 logger 與名稱／圖片記憶體快取；
- lazy credential store factory（Windows 預設使用 DPAPI，不靜默降級成明文）。

建構 runtime 只解析路徑與建立物件，不應寫入磁碟。需要寫入時由明確操作呼叫
`ensure_writable_dirs()`。Production 使用 `DEFAULT_RUNTIME`；測試使用 `BackendRuntime.from_paths(...)`
注入暫存根目錄、logger、credential factory，不 monkeypatch 真實 LocalAppData globals。

### `AppServices`

`app/services/context.py` 是 UI dependency container，持有：

- `runtime`：pages、圖片快取與 workers 共用的 `BackendRuntime`；
- `backend_provider`：每次必須建立全新 `OSTBackend` 的 factory；重用仍存活 instance 會立即 fail fast，
  避免把同一個 `httpx.AsyncClient` 帶到不同 worker event loops；
- `WorkerRegistry`：強引用所有 QThread，直到真正 finished；
- `OperationCoordinator`：以資源名稱避免 OST、Steamless、Lua 等衝突操作同時執行。

Page 透過 `app/services/tasks.py` 啟動 worker。取消是 cooperative；關閉視窗不得用 `terminate()` 強殺
thread。圖片下載則由 process-wide `ImageLoader` 合併請求並在 GUI event loop 交付結果。

## Domain boundaries

| Domain | 模組 | 邊界 |
| --- | --- | --- |
| Steam catalog | `backend/catalog/steam_store.py` | 搜尋、推薦、app info、名稱與 header cache |
| Library sources | `backend/library/sources/` | SteamCMD manifest、Sudama keys/cache、Ryuu Lua；所有遠端資料先驗證 |
| Lua library | `backend/library/service.py` | 寫入、入庫流程、列舉、移除與 Lua ownership |
| OST lifecycle | `backend/installers/ost.py` | 偵測、交易式安裝、legacy adoption、解除安裝 |
| Steamless lifecycle | `backend/installers/steamless.py` | ownership、工具目錄切換、status、移除與 recovery journal |
| Steamless runner | `backend/installers/steamless_runner.py` | 子行程、timeout／cancel、輸出 PE 與原檔替換 |
| KRKRZ | `backend/installers/krkrz.py` | bundled DLL、legacy backup 選擇、部署與還原 |
| Tool updates | `backend/tool_updates.py` | release fetch → verified download → installer 的 application workflow |

UI page 只處理呈現、使用者輸入、signals 與 worker 啟動；不得自行解 ZIP、寫 ownership manifest 或呼叫
`subprocess` domain 細節。可重用的 page 視覺元件放 `app/widgets/`，長時間循序工作放 `app/workers/`。

## Infrastructure boundaries

- `constants.py` 是 data-only leaf，不依賴 runtime 或 domain。
- `storage.py` 提供原子 JSON、跨 thread／process lock、credential stores 與 `AppPaths`。
- `security.py` 提供純驗證與安全 ZIP extraction；不做 UI、網路或產品流程。
- `deployment.py` 提供 ownership manifest、原檔備份、atomic replace、journal recovery 與 directory swap。
- `http.py`、`releases.py` 處理 HTTPS allowlist、回應上限、SHA-256、release 正規化與 asset selection。
- `config.py`、`cache.py` 管理 runtime-scoped JSON；`resources.py` 只尋找唯讀 bundled resources。
- `platform/` 封裝 Windows／Steam／PE 細節；`logging_setup.py` 負責正式 logger handlers。

## Ownership 與安全資料流

所有外部資料均視為不可信：

```text
remote / archive / existing target
  → host、大小、digest、格式與路徑驗證
  → 私有 staging
  → ownership / 原檔快照 / journal prepared
  → atomic publish
  → hash 與 manifest 再驗證
  → commit；失敗則 rollback 或下次啟動 recovery
```

核心規則：

- 遠端 Lua 只允許明確的資料指令 grammar；下載成功不代表可執行。
- GitHub 資產需要受允許 repo／host、宣告大小與 SHA-256；舊資產只接受程式碼中審閱過的 digest。
- ZIP 在寫入前完整 preflight，拒絕 traversal、duplicate、link／reparse point 與解壓大小超限。
- DLL／EXE 在部署或執行前驗證 PE 形狀，Steamless 執行前再驗證整棵 ownership tree。
- 刪除權限來自 ownership manifest 與目前 hash，不來自「檔案存在」或可過期的 UI cache。
- 第一次部署保存不可變的原檔；更新不能覆寫該備份。無法證明 ownership 時 fail closed。
- 多檔操作使用 file lock、staging、atomic replace 與持久 journal；cleanup 失敗不得假裝 rollback 已完成。

## Compatibility facades

`backend/ost_backend.py` 與 `app/ost_app.py` 是歷史 import 路徑，只做靜態 re-export。它們存在是為了：

- 保持桌面 UI、第三方整合與舊測試的 import／函式簽名；
- 讓 Nuitka 靜態看見 production modules；
- 允許漸進式遷移，而不一次破壞所有 caller。

新業務邏輯不得加回 façade。新增公開能力時，先在正確 domain 實作與測試，再視相容需求加入明確 alias、
`__all__` 與 `test_public_contract.py`。Domain module 不得反向 import 這兩個 façade。

## Dependency direction

允許的主要方向：

```text
main → app bootstrap/shell → pages/services/workers → backend application API
                                                    → domain services
                                                    → infrastructure/leaf modules
```

約束如下：

- backend 不 import `app` 或 Qt；基礎 storage/security/deployment 不依賴產品 UI。
- installer、catalog、library 等 domain 可依賴 runtime 與 infrastructure，不依賴 `backend.ost_backend`。
- pages 可呼叫 backend 支援的 application API，但不越過 domain 直接操作 manifest／store internals。
- 需要環境或外部服務的邊界用 constructor／callable 注入；測試 patch implementation module，而非期待 façade
  global patch 穿透到另一個 module。
- 跨 domain workflow 放 application service（例如 `ToolUpdateService` 或 `OSTBackend`），不要讓兩個 domain
  互相循環 import。

## 測試分層與變更清單

測試由內向外分為：純 validator/helper、storage/deployment adversarial、domain integration、Qt offscreen、
compatibility contract、真實 Windows release smoke。一般 pytest 不得碰使用者 Steam、LocalAppData 或真實網路。

新增功能時：

1. 先選定 domain 與資料 ownership，避免只按檔名隨意放置。
2. 將路徑、HTTP、時間、logger、backend provider 等外部邊界設為可注入。
3. 為不可信輸入、部分失敗、取消／重啟與 legacy state 定義 fail-closed 行為。
4. 先加 focused tests，再跑 `python -m ruff check .` 與 `python -m pytest`。
5. 若改公開 import／簽名，更新 compatibility façade 與 public-contract tests。
6. 發佈前另跑真實 Windows UI、Steam 測試目錄與 Nuitka binary smoke test。
