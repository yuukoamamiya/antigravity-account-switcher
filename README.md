# Antigravity 账号切换器

一个面向 Windows 的桌面工具，用于管理 Antigravity 的本地账号凭据、切换当前账号，并查看各账号的额度状态。程序使用 Python 标准库和 Tkinter，不需要安装第三方 Python 包。

## 环境与启动

- Windows 10/11
- Python 3.10 或更新版本（需要 Tkinter；官方 Windows 安装包通常已包含）
- 已安装并登录过 Antigravity，且本机存在可用的账号凭据文件

双击 `启动账号切换器.cmd` 或 `启动账号切换器.vbs` 启动。也可以在仓库目录运行：

```powershell
python antigravity_account_switcher.py
```

启动脚本会从项目目录运行程序。程序入口保留为 `antigravity_account_switcher.py`，便于兼容现有命令行和快捷方式。

## 本地数据

个人运行数据应放在被 Git 忽略的 `local-data/` 目录中：

```text
local-data/
├── account-switcher.config.json  # 本机配置（可选）
├── credentials/                  # 账号凭据 JSON 文件
├── quota-cache.json              # 额度查询缓存
└── account-switcher.log          # 运行日志
```

目录和配置文件可按需创建。凭据、配置、日志和缓存都属于本机数据，不要提交到版本库或粘贴到公开渠道。程序默认读取 `local-data/account-switcher.config.json`；若该文件不存在，会使用内置默认值，并兼容读取旧位置的 `account-switcher.config.json`。可复制 `account-switcher.config.example.json` 到上述本地路径后编辑。

## 配置

配置项见 [`account-switcher.config.example.json`](account-switcher.config.example.json)。相对路径以项目目录为基准，绝对路径也可使用。

- `credentialsDirectory`：账号凭据目录，默认 `local-data/credentials`。
- `antigravityExecutable`：Antigravity 可执行文件名称或路径。
- `proxy` 与 `noProxy`：额度查询使用的代理和绕过地址；示例配置默认不启用代理。
- `logFile`、`quotaCacheFile`：日志和额度缓存路径。
- `queryQuota`：启动时及手动刷新时是否查询额度。
- `requestTimeoutSeconds`、`quotaDeadlineSeconds`、`quotaMaxWorkers`、`closeTimeoutSeconds`：网络请求 timeout、每账号查询预算、并行账号数和关闭程序等待时间。工作线程数必须为 1 到 4，查询预算默认为 45 秒。
- `oauthClientId`、`oauthClientSecret`：OAuth 客户端设置。空客户端 ID 会回退到内置默认值；`ANTIGRAVITY_OAUTH_CLIENT_SECRET` 环境变量优先于文件配置。不要把个人密钥写入提交内容。

## 刷新与切换

启用 `queryQuota` 时，启动后会后台查询账号额度；也可使用界面中的“刷新额度”按钮再次查询。额度结果会写入本地缓存，供界面显示。每个账号的查询预算为 45 秒，用于限制后续尝试和各请求的 socket timeout；DNS 解析或单次阻塞读取可能超过预算，因此不保证硬实时。项目/接口路由提示只保存在当前进程内，TTL 为 6 小时，不写入额度缓存。查询需要网络访问 Google 的 Antigravity 额度服务，失败时界面会显示查询状态；关闭 `queryQuota` 可停用额度请求。

切换账号会先处理正在运行的 Antigravity 实例，再将目标账号凭据设为当前凭据。请保存 Antigravity 中未完成的工作后再切换。程序也提供 `--self-test` 诊断入口和 `--quota-only` 命令行入口。`--quota-only` 会发起真实网络请求，只在你明确需要时手动运行。`--self-test` 会扫描本机账号文件并读取 Windows Credential Manager，还会输出账号邮箱等信息；不要将它当作无凭据测试或分享其输出。

## 安全与隐私

账号凭据含有敏感认证数据，应只保存在本机。仓库忽略 `local-data/`、配置、日志和缓存路径。日志文本会过滤常见 token 字段；使用日志进行排障前仍应检查内容并遮蔽个人信息。不要提交凭据、真实配置、日志或安全扫描报告，也不要在问题或截图中暴露 token、邮箱等个人数据。

程序仅面向 Windows：账号凭据切换涉及 Windows Credential Manager，桌面界面使用 Tkinter。不要在不了解影响时手动编辑凭据文件或 Windows 凭据。

## 测试与诊断

自动化测试使用 Python `unittest`，在仓库根目录运行：

```powershell
python -m unittest discover -s tests -v
```

单元测试应能在没有真实账号凭据的环境中运行：使用临时目录、模拟凭据及模拟系统接口，不读取个人 `local-data/`，不访问真实 API，也不修改 Windows Credential Manager。`--self-test` 是面向已配置本机的诊断命令，会显示账号邮箱等数据，不属于上述测试流程。

## 目录与后续维护

运行入口和 `.cmd`/`.vbs` 启动器保持在仓库根目录，确保既有启动方式继续可用。代码按职责放在少量 Python 模块中：`config.py` 验证和加载配置，`common.py` 提供共享工具，`account_core.py` 管理账号与凭据，`switcher_ui.py` 实现桌面界面；`process_control.py`、`runtime_control.py`、`quota_client.py` 和 `cache_store.py` 分别处理进程、运行时协调、额度网络请求和额度缓存。测试集中放入 `tests/`，不依赖第三方包。

`reference/antigravity-electron/` 中的 `main.js`、`paths.js`、`constants.js` 是 Antigravity Electron 程序的参考材料，不属于本账号切换器的执行入口，也不是本项目的运行依赖。
