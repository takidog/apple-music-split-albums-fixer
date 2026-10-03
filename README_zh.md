# Apple Music 分割專輯修復工具

[English](README.md)

這是一個 Python CLI，用來找出並修復被 Apple Music 拆成多筆本機專輯紀錄的專輯。目前版本支援 Windows 版 Apple Music。

主程式會掃描目前的音樂資料庫，只列出可信度高、能安全處理的候選項目，讓使用者用編號選擇專輯。執行修復時，它會建立完整的還原備份、修復本機資料庫，並將選中的變更同步到「雲端音樂資料庫」（Cloud Library）。

## 系統需求

- Windows 10 或 Windows 11
- Windows 版 Apple Music
- [uv](https://docs.astral.sh/uv/)
- 若要同步雲端，需要近期且已通過驗證的 mitmproxy 封包檔，以及相容的 `sapsigner.exe`

Python 相依套件已宣告在各個腳本內，`uv` 會在執行時自動安裝。

## SAP signer 來源與安裝方式

SAP signer 不是 Apple Music Split Albums Fixer 自行提供的執行檔。其上游原始碼是 [t0rr3sp3dr0/sapsigner](https://github.com/t0rr3sp3dr0/sapsigner)，採用 [Apache-2.0 授權](https://github.com/t0rr3sp3dr0/sapsigner/blob/master/LICENSE)。

本專案測試使用的 Windows 預編譯套件來自 [pdx15/ipatool-webGUI](https://github.com/pdx15/ipatool-webGUI)。該專案採用 MIT 授權，並在 Windows 工具套件中使用 `sapsigner.exe` 產生 SAP action signature。使用者可以從它的 [GitHub Releases](https://github.com/pdx15/ipatool-webGUI/releases) 下載 Windows 壓縮檔，或將儲存庫 clone 到本專案的 `vendor` 目錄：

```powershell
git clone --depth 1 https://github.com/pdx15/ipatool-webGUI.git .\vendor\ipatool-webGUI
```

主 CLI 會自動尋找：

```text
vendor\ipatool-webGUI\tools\sapsigner.exe
```

請保留下載套件內完整的 `tools` 目錄。Signer 還需要同一目錄中的 `libunicorn.dll`、`ucworker.dll` 與 `sap-cache` 目錄；只複製 `sapsigner.exe` 無法執行。若將 release 解壓縮到其他位置，請明確指定路徑：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py `
  --sap-signer 'C:\path\to\ipatool-webGUI\tools\sapsigner.exe'
```

開發期間實際測試的執行檔來自 `pdx15/ipatool-webGUI` commit [`0adb7072dc638c24913beef41c6cc92cc4aa4e6b`](https://github.com/pdx15/ipatool-webGUI/tree/0adb7072dc638c24913beef41c6cc92cc4aa4e6b)，其 SHA-256 為：

```text
A8A536DBBE3BD4E179C988B799E9E4F1CBFFB6DF787C8433AAFE2E0B7AC5A920
```

可使用 PowerShell 驗證：

```powershell
(Get-FileHash '.\vendor\ipatool-webGUI\tools\sapsigner.exe' -Algorithm SHA256).Hash
```

## 互動式修復

在專案目錄執行：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py
```

工具會自動尋找 Windows 上的標準音樂資料庫位置，並顯示類似以下內容：

```text
Found 3 high-confidence split album(s):

[ 1] Branch — やなぎなぎ | 2 parts, 4 tracks, move 1
[ 2] Follow My Tracks — やなぎなぎ | 2 parts, 14 tracks, move 2
[ 3] Green Light — やなぎなぎ | 2 parts, 4 tracks, move 1

Select album numbers (example: 1,3-5) or type all:
```

選擇完成後，工具會依序：

1. 在修改資料庫前檢查雲端封包與 signer；
2. 詢問後才關閉 Apple Music；
3. 在 Apple Music 停止後重新掃描資料庫，避免使用已失效的紀錄 ID；
4. 備份完整的 `.musiclibrary` 目錄；
5. 建立修復後的資料庫，重新解析並確認結構正確；
6. 以不可分割的原子操作安裝通過驗證的資料庫；
7. 只替選中的曲目送出已簽署的 Cloud Library 修改；
8. 讀回雲端 delta，報告相符、遺漏及內容不同的紀錄數量。

預設備份與報告目錄為：

```text
%USERPROFILE%\Music\Apple Music Split Albums Fixer Backups\YYYYMMDD-HHMMSS
```

其中包含完整還原副本、本機修復交易報告、通過驗證的修復資料庫，以及雲端驗證結果。

## 常用模式

只列出目前的問題，不寫入任何資料：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py --list
```

預覽選擇結果，不寫入任何資料：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py --select 1,3-5 --dry-run
```

修復所有安全候選項目，但略過雲端同步：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py --all --local-only
```

明確指定雲端同步所需檔案：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py `
  --capture '.\captures\recent-sync.mitm' `
  --sap-signer 'C:\path\to\sapsigner.exe'
```

自動化執行時，必須明確指定選擇範圍與同意參數：

```powershell
uv run --python 3.12 .\tools\apple_music_split_albums_fixer.py `
  --select 1,2 `
  --yes `
  --stop-apps `
  --restart `
  --capture '.\captures\recent-sync.mitm' `
  --sap-signer 'C:\path\to\sapsigner.exe'
```

資料庫位於非標準位置時，可使用 `--database`。省略 `--capture` 時，程式會在專案的 `captures` 目錄尋找最新的 `.mitm` 檔案。

程式會依序在 `--sap-signer` 指定位置、`APPLE_MUSIC_SAP_SIGNER` 環境變數、專案根目錄、`tools\sapsigner.exe`、開發環境的 `work\vendor\ipatool-webGUI` 目錄，以及已知的 Signum 安裝路徑尋找 signer。互動模式若仍找不到，會要求使用者輸入檔案路徑。

`--offline-copy` 可用來測試與 Apple Music 分離的資料庫副本，而不會停止或啟動 Apple Music。請勿對正式使用中的音樂資料庫使用此參數。

## 修復原理

Apple Music 的 `Library.musicdb` 是經過加密與壓縮的二進位資料庫。修復工具會：

1. 驗證 `hfma` 外層資料結構；
2. 解密 AES-128 ECB 前段並解壓縮 zlib 資料；
3. 解析每一筆 `itma` 曲目與 `iama` 專輯紀錄；
4. 根據正規化後的專輯名稱、專輯藝人及藝人名稱將專輯物件分組；
5. 只有在其中一筆紀錄擁有唯一多數、曲目編號沒有重複、顯示資訊完全一致，而且合併後曲目編號連續時，才判定能安全修復；
6. 將少數分支的曲目重新指向主要專輯物件，並移除不再使用的專輯紀錄；
7. 更新紀錄數量、區段長度、時間戳與外層資料結構；
8. 重新加密並再次解析結果，確認正確後才取代正式資料庫。

若出現票數相同、曲目編號重複或缺漏、顯示資訊不一致等情況，工具會將其列為模糊候選，不提供自動修復。

雲端同步會從修復前的備份，將變更曲目的本機 ID 對應到 Cloud Library 的 `miid`。每一個請求都會產生新的 SAP `X-Apple-ActionSignature`。工具會先暫時切換「專輯是合集」欄位，再以第二個請求恢復預期值，接著下載 `/items` delta 並逐筆驗證目標資料。

## 還原

關閉 Apple Music 與 `AMPLibraryAgent`，再使用相應時間目錄中 `backup` 內的資料取代目前的 `.musiclibrary` 目錄。請保留備份，直到修復後的音樂資料庫已成功重新啟動 Apple Music，並在另一台裝置確認雲端結果。

## 進階工具

以下底層腳本仍可用於檢查及手動流程：

- `tools/musicdb_duplicate_scanner.py`：產生唯讀的 JSON 與 Markdown 掃描報告；
- `tools/musicdb_duplicate_repair.py`：將修復後的資料庫寫入另一個輸出路徑；
- `tools/sync_repaired_albums.py`：預覽或套用雲端交易報告。

掃描器、修復規劃器、二進位編碼器及雲端同步器都是不依賴 Windows UI 的 Python 模組。未來的 macOS 版本可以沿用這些模組，只需新增 macOS 資料庫定位及 Music 程序控制。

協定細節與逆向研究筆記保存在 [research/README.md](research/README.md)。

## 安全機制

- Apple Music 執行期間不會修復正式資料庫。
- 安裝修復結果前，一定會建立完整的還原副本。
- 模糊的重複專輯群組不會被自動修改。
- 原始封包可能包含帳號 token 與音樂資料庫識別碼，請妥善保管。
- `sapsigner.exe` 是外部元件，本專案不會散布該檔案。

本專案是獨立的互通性工具，與 Apple Inc. 沒有從屬關係。Apple Music 及相關名稱的商標權屬於各自權利人。
