# IceHub (OSTInstall)

[![CI](https://github.com/iceorange-dev/IceHub/actions/workflows/ci.yml/badge.svg)](https://github.com/iceorange-dev/IceHub/actions/workflows/ci.yml)
[![Release](https://github.com/iceorange-dev/IceHub/actions/workflows/release.yml/badge.svg)](https://github.com/iceorange-dev/IceHub/actions/workflows/release.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

IceHub 是 Windows 桌面工具，用來管理 OpenSteamTool、產生及更新 Steam Lua 清單、執行
Steamless，以及部署 KRKRZ 輔助 DLL。專案目前以 Windows 和 Python 3.12 為唯一支援環境。

> 請只對你有權處理的軟體與帳號使用這些功能，並遵守當地法律、Steam 條款及上游專案授權。

IceHub 與 Valve、Steam、OpenSteamTool、Steamless 及 KRKRZ 引擎作者沒有隸屬或背書關係。

## 下載

正式 Windows 成品請只從
[GitHub Releases](https://github.com/iceorange-dev/IceHub/releases) 下載。每個 Release 同時提供
`SHA256SUMS.txt`；執行前可使用以下命令核對：

```powershell
Get-FileHash -Algorithm SHA256 .\IceHub.exe
Get-Content .\SHA256SUMS.txt
```

目前 Release EXE 沒有 Authenticode 數位簽章，因此 Windows SmartScreen 可能顯示警告。內嵌 KRKRZ
DLL 的已知來源限制與 hash 記錄於 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 開發環境

需要 Windows 10/11 x64 與 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python main.py
```

`requirements.txt` 是執行期依賴；`requirements-dev.txt` 另包含 Nuitka、pytest 與 Ruff。依賴使用已審閱
的 lower bound 和明確 upper bound，不代表所有組合都已通過測試；`requirements-release.txt` 則固定
正式 Windows release pipeline 的完整環境。發佈前仍應在乾淨環境重跑 CI。

## 架構速覽

新程式碼已依責任拆分；完整的依賴規則、安全邊界與資料流請見
[ARCHITECTURE.md](ARCHITECTURE.md)。最上層流程如下：

```text
main.py
└─ app/bootstrap.py                 # QApplication、theme、單一實例
   └─ app/shell/main_window.py      # AppServices 與六個頁面的 composition root
      ├─ app/pages/                 # 每個功能頁各自一個模組
      ├─ app/widgets/、workers/     # 可重用 widget 與長時間 QThread 工作
      └─ backend/facade.py          # OSTBackend application facade
         ├─ backend/catalog/        # Steam 商店查詢
         ├─ backend/library/        # Ryuu/Sudama/SteamCMD 與 Lua ownership
         ├─ backend/installers/     # OST、Steamless、KRKRZ
         └─ backend/tool_updates.py # release 更新流程
```

主要模組地圖：

| 區域 | 責任 |
| --- | --- |
| `backend/runtime.py` | 注入路徑、JSON stores、logger、credential factory 與記憶體快取 |
| `backend/security.py` | 遠端 Lua、depot key、manifest GID、GitHub repo、ZIP 路徑等純驗證 |
| `backend/storage.py`、`deployment.py` | 原子 JSON、跨行程 lock、ownership manifest、rollback／recovery |
| `backend/http.py`、`releases.py` | 有上限的 HTTP、digest 驗證、GitHub release 正規化與版本選擇 |
| `app/services/`、`app/tasking.py` | `AppServices`、worker ownership、資源互斥與圖片載入 |
| `app/ost_app.py`、`backend/ost_backend.py` | 舊 import 路徑的靜態相容 façade；不放新業務邏輯 |

### 新功能應放哪裡

- 新畫面放 `app/pages/`；跨頁重用的 UI 放 `app/widgets/`。
- 會阻塞或需取消的 Qt 工作放 `app/workers/`，並透過 `app/services/tasks.py` 註冊；不要在 page
  直接管理無 owner 的 thread。
- Steam 商店資料放 `backend/catalog/`；新的 Lua 資料來源放 `backend/library/sources/`；Lua 寫入、列舉與
  移除放 `backend/library/service.py`。
- 新工具的安裝／偵測／移除放 `backend/installers/`，共同交易與 ownership 能力放
  `backend/deployment.py`，不要自行實作另一套備份規則。
- release 更新編排放 `backend/tool_updates.py`；HTTP、digest 或資產挑選等通用能力分別放
  `backend/http.py`、`backend/releases.py`。
- 路徑與 stores 由 `BackendRuntime` 注入；設定、快取、平台整合與純安全驗證分別放
  `config.py`、`cache.py`、`platform/`、`security.py`。
- 只有需要維持既有公開 import 時，才在 `app/ost_app.py` 或 `backend/ost_backend.py` 增加靜態
  re-export，並同步更新 public-contract tests。

## 使用者資料

可寫資料不應放在程式安裝目錄，預設位於：

```text
%LOCALAPPDATA%\IceHub\
├─ config\       # 設定與本機狀態
├─ cache\        # 可重新取得的快取
├─ logs\         # 診斷日誌
├─ temp\         # 下載／部署 staging
└─ tools\        # 下載的外部工具
```

若程式同層存在 `portable.flag`，改用程式同層的 `data\` 作為資料根目錄。Portable
模式仍需確保該目錄可寫，並且不應將 `data\` 連同認證資料打包或分享。

資源檔（圖示、內嵌 DLL）是唯讀程式資源。Steam 的 Lua 檔會寫入
`<Steam>\config\lua\`，不在 IceHub 資料目錄內。設定可能含 Ryuu 認證資料；不要將本機設定、
日誌或資料目錄加入版本控制，也不要在 issue 中直接貼上。

## 安全模型

IceHub 會下載外部資料、寫入 Steam 目錄，並可啟動 Steamless，因此不把網路輸入、ZIP
檔名或目標目錄視為可信。核心邊界如下：

- 遠端 Lua 必須完整符合有限的白名單資料指令（含嚴格數值／金鑰格式），任何其他 Lua 語法皆拒絕；
  不接受或執行任意遠端 Lua。
- ZIP 只可在 staging 目錄安全解壓；應拒絕絕對路徑、`..`、symlink／reparse point、超過限制的大小與壓縮比。
- OST／Steamless／KRKRZ 部署必須用 ownership manifest 和 hash 辨識 IceHub 管理的檔案；
  使用 staging、atomic replace 與 rollback，避免移除使用者原有檔案。
- HTTPS 不等於可驗證的來源。上游若沒有簽章或公開 checksum，應在發佈前人工審閱、固定版本，
  並留存審閱過的 SHA-256。
- 同一時間只應有一個會改動相同資源的作業。作業中關閉應先取消／等待 worker，必要時終止子行程。

「已入庫」選單可對已管理的 AppID 重新取得並更新 Lua。更新仍需經過相同驗證，
並以原子取代寫入；不應將「檔案存在」當作內容可信的證明。

## 測試與靜態檢查

```powershell
python -m ruff check .
python -m pytest
```

GitHub Actions 在 `windows-latest` 和 Python 3.12 執行這兩項檢查。Qt 在 CI 使用 offscreen platform，
不代表已覆蓋所有互動式 GUI 行為；發佈前還需在真實 Windows 桌面執行 smoke test。

測試依下列層次安排，失敗時應先從最靠近修改處的層次開始：

1. **純單元測試**：constants、格式解析、security 與不接觸磁碟的 helper。
2. **儲存與對抗測試**：atomic write、lock、ownership、rollback、ZIP／link／路徑竄改。
3. **Domain integration**：以 `BackendRuntime.from_paths(...)`、暫存目錄及 mock HTTP 測 catalog、來源、
   library、installer 與 tool update。
4. **Qt offscreen**：page／widget constructor、signals、worker cancel、關閉生命週期與空狀態。
5. **相容契約**：`test_public_contract.py` 驗證舊 façade exports 與呼叫簽名。
6. **發佈 smoke**：真實 Windows 桌面、Steam 測試目錄、Nuitka build、最終 binary hash／簽章；不在一般
   unit tests 操作使用者的 Steam 或 LocalAppData。

新增測試時，優先直接 import 所屬 domain，而不是透過相容 façade；只有相容性本身才測 façade identity。

## 建置

建置前先核對內嵌第三方 DLL：

```powershell
Get-FileHash -Algorithm SHA256 .\assets\krkrz\Version.dll
python build_exe.py
```

預期輸出為 `dist\IceHub.exe`。`build_exe.py` 使用 Nuitka onefile 與 PyQt6 plugin，建置過程可能需下載
C compiler 元件。推送 `v*` tag 時，[release workflow](.github/workflows/release.yml) 會在乾淨的 Windows
runner 執行 lint、完整測試、建置與 `--verify-bundle`，再產生 SHA-256 並建立 GitHub Release。現有流程
尚未提供 Authenticode 簽章，也不宣稱 bit-for-bit reproducible build。

內嵌 `Version.dll` 的來源、實測 hash、上游參考版本與尚未解決的 provenance 限制記錄在
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。Release 保留該 DLL，但不把目前檔案宣稱為可由某個
確切 upstream commit 重現；使用者與再散布者應自行評估該限制。

## 授權

IceHub 自有原始碼以 [GNU General Public License v3.0 only](LICENSE) 發佈。第三方套件、執行期下載內容
與內嵌 binary 仍適用各自的授權與聲明，詳見 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

安全問題請依 [SECURITY.md](SECURITY.md) 私下通報，不要在公開 issue 張貼認證資料或未脫敏日誌。
