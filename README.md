# mihomo-server

为 Linux 无 GUI 开发服务器提供 Mihomo 服务管理、订阅安全更新、自动节点回退和网络诊断。

**默认机器已安装 Mihomo。** 本工具只管理自己的配置和用户级服务；不修改 SSH、防火墙、系统 DNS，也不启用 TUN。不需要 sudo。安装不会关闭或接管已有代理，端口或文件冲突时会退出。

当前为 `0.1.0` 初版。在 Mihomo v1.19.25 上验证配置兼容性；其他版本请以安装时的 `mihomo -t` 和连接测试为准。要求 Linux、Python 3.9+（含 venv/pip）、curl；后台自动管理还需要可用的 systemd 用户管理器。

## 安装

在目标服务器克隆或复制本仓库。不要把原机器的私有配置放进 Git。

从本地 Clash/Mihomo YAML 导入节点，并填写订阅地址（隐藏输入）：

```bash
./install.sh --mihomo /path/to/mihomo \
  --nodes /path/to/existing-config.yaml \
  --prompt-subscription --activate
```

也可以只输入订阅地址，由安装器下载初始节点：

```bash
./install.sh --mihomo /path/to/mihomo --prompt-subscription --activate
```

若 `mihomo` 已在 PATH 中，可以省略 `--mihomo`。订阅需返回包含 `proxies` 列表的 YAML；不支持只包含远程 provider 的配置、Base64 节点列表或订阅转换服务。

`--activate` 会先在独立端口运行 Mihomo 并测试配置中的服务，通过后再启用后台服务和更新定时器，并验证正式端口。没有该参数时只安装和执行配置语法校验，不启动服务、不做外网连接测试。

没有订阅也可使用 `--nodes FILE`，此时保留静态节点，安装激活时不会启用更新定时器。以后在私有 `settings.json` 中填写订阅并执行：

```bash
mihomo-server update
systemctl --user enable --now mihomo-server-update.timer
```

安装使用独立 Python venv，仅安装锁定版本的 PyYAML；下载依赖和订阅默认忽略代理环境变量。Mihomo 本身不由本仓库下载。安装器不覆盖已有安装；升级前应备份私有配置并按照 [维护说明](docs/operations.md) 操作。

## 日常使用

```bash
source ~/.config/mihomo-server/shell.sh
proxy on
proxy doctor
# 正常运行 codex、git、W&B 等工具
proxy off
```

若希望新开的交互式 Bash 终端自动提供命令，在自己的 `.bashrc` 添加下面一行。它只定义函数，**不会自动开启代理**：

```bash
[ ! -r "$HOME/.config/mihomo-server/shell.sh" ] || . "$HOME/.config/mihomo-server/shell.sh"
```

`on/off` 必须由 sourced shell 函数执行，子进程不能修改父 shell。独立程序 `~/.local/bin/mihomo-server` 可执行其余所有管理命令；确保 `~/.local/bin` 在 PATH 中。`proxy off` 清除当前 shell 的代理变量，不停止后台服务，也不还原之前其他代理的变量值。已有 `no_proxy/NO_PROXY` 条目会保留。

| 命令 | 功能 |
|---|---|
| `proxy start / stop / restart` | 管理专用用户服务 |
| `proxy status` | 服务状态、实际路由、节点、上游 DNS、健康检查和最近故障 |
| `proxy test` | 有超时限制的真实 HTTPS/服务检查 |
| `proxy doctor` | 状态与网络测试，并给出故障类别提示 |
| `proxy update` | 下载、隔离验证、备份并安全应用订阅 |
| `proxy logs` | 最近服务与更新日志 |
| `proxy on / off` | 当前 shell 启用/关闭代理 |

## 端口与可选服务

默认代理为 `127.0.0.1:17890`，控制器为 `127.0.0.1:19090`，隔离验证使用 `27890/29090`；四个端口都只使用 localhost，必须互不相同且未被占用。

```bash
./install.sh --nodes /path/to/config.yaml \
  --port 17891 --controller-port 19091 \
  --stage-port 27891 --stage-controller-port 29091 \
  --services openai,wandb --activate
```

`--services openai,wandb` 是默认值。可使用 `--services openai`，或 `--services ''` 只检查通用 HTTPS 与 GitHub。无凭据的 OpenAI API 预期返回 401；W&B 根路径允许 404。这些验证传输可达性，不证明账号、模型权限、SDK 操作或 Codex 登录一定可用。

## 设计

- 规则模式：OpenAI 域名 → OPENAI，W&B 域名 → WANDB，其他公网流量 → AUTO；loopback/RFC1918 IP 走 DIRECT。可选组关闭时对应域名走 AUTO。
- 原生 fallback 使用固定优先顺序，而非最低延迟。更新保留原有排序，新节点追加；恢复的高优先级节点可以重新使用。
- 原生 provider 每 180 秒检查通用 HTTPS 和启用的服务。不同 URL 有独立健康状态，能区分普通网络可达与服务兼容性。
- 所有上游仍遵循订阅的传输/TLS设置；目标 HTTPS 使用正常证书验证。安装器不会修改系统解析器。
- 订阅每六小时更新，带最多五分钟抖动；systemd 更新失败后十五分钟重试。无变化不重载。
- 自定义 Python 仅补充 native HTTP provider 之外的事务能力：下载限制、Mihomo 校验、独立实例的服务测试、原子替换、八份备份、应用失败回退。
- 服务自动重启，日志在进入 journald 前屏蔽 URL 和已知凭据。

不是所有 HTTP 403 都是断网；诊断会将它标记为服务限制。启用的 OpenAI/W&B 检查会把不符合预期的响应视为该服务不兼容。健康检查需要一轮扫描时间，现有失败连接需要应用重试；所有节点都失效时无法保证联网，原生 fallback 会尝试第一个节点。

原生行为参考：[fallback](https://github.com/MetaCubeX/mihomo/blob/v1.19.25/adapter/outboundgroup/fallback.go)、[provider health-check](https://github.com/MetaCubeX/mihomo/blob/v1.19.25/adapter/provider/healthcheck.go)。

## 开机运行

```bash
loginctl show-user "$USER" -p Linger
systemctl --user status mihomo-server.service
systemctl --user list-timers mihomo-server-update.timer
```

若 lingering 未开启，退出最后一个登录会话后或开机时，用户服务可能不会持续运行。由用户或管理员决定是否启用；本工具不会自动更改该设置。

## 私有数据与卸载

安装后的订阅 URL、节点密码、随机控制器密钥、备份和状态位于 `~/.config/mihomo-server/`，不在仓库中。配置目录权限为 0700，私有文件为 0600。不要上传该目录或将订阅 URL 作为命令行参数。

```bash
./uninstall.sh
```

卸载停止属于本安装的服务，删除清单记录且内容未修改的安装文件，保留私有配置、节点与备份。若命令或 unit 被修改，会拒绝卸载以保护改动。自行移除 `.bashrc` 中添加的 source 行并重新打开 shell。它不会恢复安装前已存在的其他代理，因为安装本身不会接管这些服务。

## 开发与测试

```bash
python3 -m venv .venv
# 下载依赖时忽略当前 shell 的代理变量
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
     -u all_proxy -u ALL_PROXY .venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

测试使用临时 HOME、合成节点和 mock，不访问真实订阅、不启停宿主机代理。若要运行两个真实 Mihomo 校验回归用例，可在上述测试命令前设置 `MIHOMO_TEST_BINARY=/path/to/mihomo`。详细边界与验证记录见 [维护说明](docs/operations.md)。

此仓库不包含个人订阅、节点文件或第三方 Mihomo 二进制；默认也不会修改 shell 启动文件。本地仓库可以直接复制到另一台机器安装。
