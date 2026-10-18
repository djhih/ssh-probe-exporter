# probe — SSH 登入/登出與指令延遲探測

從**monitoring 端**(跑 Prometheus 的那台)往多台 target 主動打 SSH,量「使用者體感」:
登入要等多久、登出要等多久、登入後敲個指令要多久。這是整個 stack 裡唯一一個
**不裝在 target 上**的 exporter —— 它量的就是「從外面連進去」這件事,
裝在 target 上就量不到了。

## 設定(寫死在檔案裡)

帳號、密碼、target IP 清單直接改 `ssh_probe_exporter.py` 開頭的設定區塊:

```python
TARGETS = [
    "gpu-node-1",
    # "192.0.2.11",     # 多台就往下列
]
SSH_USER = "probeuser"
SSH_PASSWORD = ""        # 空字串 = 用 key;填了 = 用密碼(需要 sshpass)
```

- **密碼認證**需要 monitoring host 裝 `sshpass`(`sudo apt install sshpass`)。
  密碼走環境變數(`SSHPASS`)傳給 sshpass,不進 argv,`ps` 看不到。
- **⚠️ 這個 repo 在 GitHub 上是公開的**。檔案裡的值是佔位符;
  真密碼只填在部署那台的本機 checkout,**絕對不要 commit / push**。
  改完可以用 `git update-index --skip-worktree probe/ssh_probe_exporter.py`
  防止手滑。
- **TARGETS 的字串要跟其他 scrape job 的 `instance` 一字不差**
  (hostname vs IP 有差):scrape config 會把 metric 的 `target` label
  複製成 `instance`,跨 job join 是逐 byte 比對的。

## 三個探測在量什麼(以及為什麼分開)

| 探測 | 週期 | 量法 | 回答的問題 |
| --- | --- | --- | --- |
| login | 10s | 開真的 login shell(**關 multiplexing**),計時到 shell 回應第一個指令 | 新開一個登入要多久:TCP + key exchange + auth + shell 啟動全包 |
| logout | 10s(同一個 session) | 送 `exit`,計時到 ssh process 完全結束 | 打 exit 之後多久拿回本機 prompt:channel 關閉 + session 清理 |
| command | 5s | `cd` / `ls` / `poetry --version`,走**共用的 ControlMaster 連線** | 已登入的人敲指令要多久:純指令延遲,不含連線建立 |

login/logout 是同一次真實 session 的兩段:先餵 `echo __SSH_PROBE_READY__`,
marker 回來代表 shell 真的能執行指令(banner/motd 騙不了它)——這段是登入;
再餵 `exit`,量到 process 結束——這段是登出。

command 探測分開的理由:如果每次都重新握手,量出來的數字 ≈ login + 指令,
兩條線高度相關、看不出哪邊在慢。所以 command 探測先在**計時區間外**
把 master 連線養好(`ensure_master()`),計時只剩指令本身。
master 掛了也不會沉默——指令會自己失敗,反映在 `*_success` / `*_failures_total` 上。

誠實原則跟 exporter.py 一樣:**失敗的那次不發 duration**(缺席 = 失敗或還沒量,
永遠不是 0),dashboard 上會是空隙而不是一條貼地的假線。

## 部署(monitoring host 上)

```bash
# 密碼模式先裝 sshpass;key 模式要先把 key 跟 known_hosts 弄好
python3 probe/ssh_probe_exporter.py &          # 預設 127.0.0.1:9754
SSH_PROBE_LISTEN=0.0.0.0:9754 python3 probe/ssh_probe_exporter.py &   # 給 docker 裡的 Prometheus scrape
```

Prometheus 在 docker 裡,透過 `host.docker.internal:9754` 來 scrape
(compose 已加 `extra_hosts: host-gateway`),所以 LISTEN 得是
`0.0.0.0` 或 `172.17.0.1`,不能是預設的 `127.0.0.1`。

## Target 上安裝 venv 與 poetry

poetry 探測要求 target 上該帳號執行得了 `poetry --version`。從零開始:

```bash
# 1) venv 模組(Debian/Ubuntu 把它拆出去了;RHEL 系通常內建)
sudo apt install python3-venv        # Ubuntu/Debian
# sudo dnf install python3           # RHEL/Rocky 一般已含 venv

# 2) 用官方 installer 裝 poetry(裝進 ~/.local/share/pypoetry,自帶隔離的 venv)
curl -sSL https://install.python-poetry.org | python3 -

# 3) 讓非互動 shell 也找得到(installer 只會加到 ~/.profile,ssh 執行指令不讀它)
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc   # 放在 bashrc 最上面,
                                                           # 要在 "case $- in *i*)" 早退之前
poetry --version                     # 確認
ssh <帳號>@<target> 'poetry --version'   # 從 monitoring host 確認非互動也通

# (專案要用 venv 的話,額外)
python3 -m venv .venv && poetry config virtualenvs.in-project true
```

裝不進 PATH 也沒關係,直接把探測指令換成絕對路徑:

```bash
SSH_PROBE_POETRY_CMD="$HOME/.local/bin/poetry --version" python3 probe/ssh_probe_exporter.py
```

## Env 一覽(部署細節;打誰、用什麼帳密是上面的固定設定)

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `SSH_PROBE_LISTEN` | `127.0.0.1:9754` | 部署時設 `0.0.0.0:9754` |
| `SSH_PROBE_CONNECT_INTERVAL` | `10` | login/logout 探測週期(秒) |
| `SSH_PROBE_COMMAND_INTERVAL` | `5` | command 探測週期(秒) |
| `SSH_PROBE_TIMEOUT` | `10` | 單次 ssh 逾時(秒) |
| `SSH_PROBE_WORKDIR` | (家目錄) | cd/ls/poetry 的執行目錄,要給**絕對路徑** |
| `SSH_PROBE_POETRY_CMD` | `poetry --version` | 見上方 PATH 說明 |
| `SSH_PROBE_MULTIPLEX` | `1` | 設 `0` 改成每個指令都全新連線 |
| `SSH_PROBE_CONTROL_PATH` | `~/.ssh/ssh-probe-%C` | `%C` 含 host hash,各 target 不共用 master |

## 陷阱

- **poetry 找不到**:ssh 非互動 shell 常常沒有 `~/.local/bin` 的 PATH,
  見上面第 3 步;或直接用絕對路徑。
- **login 數字小得不合理**(幾 ms):代表量到了 multiplexed 連線。
  exporter 已用 `-S none` 防了 user ssh_config 裡的 ControlPath,
  出現這狀況先查是不是有別的 wrapper(如 ProxyCommand 快取)。
- **量測本身也是負載**:每台 target 每 10 秒一次完整登入/登出 + 每 5 秒
  3 個指令,對主機幾乎無感,但 sshd 的 auth log 會多很多筆,查 log 時記得濾掉。
- **密碼模式的 host key**:用 `accept-new`(第一次自動收,之後變了就硬失敗)。
  key 模式維持 BatchMode,known_hosts 要先種好。

## 健康檢查:`check_probe.py`

探測 thread 死掉時，gauge 會停在最後一次成功的值，dashboard 看起來一切正常。
`check_probe.py` 用 `last_run` 時間戳抓出這種情況，並逐台列出每個探測的最後結果:

```bash
python3 probe/check_probe.py                                   # 只看 exporter
python3 probe/check_probe.py --prometheus http://127.0.0.1:9090  # 加查 scrape 與 instance join
```

- **STALE**:某探測超過 `3 × 週期 + 2 × timeout` 沒跑 → loop 死了或卡住
- **FAILING**:最後一次失敗;`failures since start` 是 exporter 啟動以來的累計
- **slow**:成功但超過 `--slow`(預設 2s)
- **instance join**:target 字串沒有和任何其他 job 的 `instance` 一字不差 → 跨 job join
  和 `$host` 會漏掉這台;同 host 但寫法不同(例如多了 `:port`)會直接提示
- 改過 `SSH_PROBE_*_INTERVAL` / `SSH_PROBE_TIMEOUT` 的話，用同名 flag 告訴它
- exit code:0 全部 OK、1 只有 WARN、2 有 CRIT(可以接 cron 或 healthcheck)

## Metrics

- `ssh_probe_connect_duration_seconds` / `_success` / `_failures_total` —— 登入
- `ssh_probe_logout_duration_seconds` / `_success` / `_failures_total` —— 登出
  (login 失敗的那輪,logout 也記一次失敗,counter 才不會沉默)
- `ssh_probe_command_duration_seconds{command="cd|ls|poetry"}` 及同名
  `_success` / `_failures_total`
- `ssh_probe_last_run_timestamp_seconds{probe=...}` —— 抓「探測 thread 死掉」:
  gauge 凍住看起來像一切正常,只有這個時間戳會停。
- 所有 series 都帶 `target` + `user` label;scrape config 把 `target`
  複製成 `instance`(一個 exporter 回報多台主機)。

Dashboard:`monitoring/grafana/dashboards/ssh-probe.json`
(SSH Probe - Login & Command Latency),用 `$host` 變數切主機。
