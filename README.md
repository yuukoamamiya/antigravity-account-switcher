# Antigravity 账号切换器

面向 Windows 10/11 x86-64（64 位）的桌面账号管理工具。便携版无需安装 Python，管理本机账号凭据、切换当前账号，并查询额度状态。项目不隶属于 Google，也未获 Antigravity 官方认可。

## 下载与首次使用

普通用户请从 GitHub Releases 下载 `AntigravityAccountSwitcherPortable-0.1.0-win10-x64-portable.zip`，解压后运行 `AntigravityAccountSwitcherPortable.exe`。便携版适用于 Windows x86-64；不提供 32 位或 ARM 构建。

首次使用前，先安装并启动官方 Antigravity 应用，登录你要导入的账号，然后回到切换器点击“添加当前账号”。之后可在切换器设置本地代理端口（例如 `10809`）并刷新额度。代理只用于额度查询。

切换账号会关闭正在运行的官方 Antigravity 应用，再设置目标账号凭据。请先保存官方应用中的未完成工作。添加当前账号则不会关闭官方应用。

## 便携目录与本地数据

发布包采用类似 PortableApps 的目录布局；这只是目录组织方式，不代表 PAF 认证或与 PortableApps 官方关联。

```text
AntigravityAccountSwitcherPortable/
├── AntigravityAccountSwitcherPortable.exe
├── App/       # PyInstaller 支持文件
├── Data/      # 本机设置、凭据、日志和额度缓存
└── Other/     # 随包文档和许可证
```

配置和运行数据默认保存在 `Data/`：`Data/settings/account-switcher.config.json`、`Data/credentials/`、`Data/logs/account-switcher.log` 和 `Data/cache/quota-cache.json`。这些文件以明文保存在本机，不加密。请保护整个目录，不要分享或上传它，也不要将个人数据粘贴到问题、截图或日志中。已有 `local-data/` 私有配置仍可兼容读取；程序不会主动迁移或删除该目录。新电脑上需要先登录官方 Antigravity，再使用“添加当前账号”导入凭据。

“便携”表示程序可从解压目录运行，不保证不写入系统位置：切换账号功能会读取和写入 Windows Credential Manager，这是功能所需行为。额度刷新使用 Google OAuth 和非公开额度接口，并保持 TLS 证书验证。接口或授权要求可能变化，因此不能保证新用户一定能成功刷新；发生 OAuth 刷新失败时，额度状态会显示错误。若需要自备 OAuth 参数，请仅提供你有权使用的值：`oauthClientSecret` 配置项或 `ANTIGRAVITY_OAUTH_CLIENT_SECRET` 环境变量。发行包不携带个人 OAuth 参数。关闭额度查询可在配置中设置 `queryQuota` 为 `false`。

升级时先退出切换器，将新版压缩包解压到临时目录，然后用新版 `AntigravityAccountSwitcherPortable.exe`、`App/` 和 `Other/` 替换旧版本对应内容，保留原有 `Data/`。替换前建议关闭应用并备份 Data。不要用新包中的空 Data 覆盖已有数据。

## 从源码运行

源码运行需要 Windows 10/11 x86-64、Python 3.10 或更新版本及 Tkinter。无需安装第三方 Python 包：

```powershell
python antigravity_account_switcher.py
```

首次运行先启动官方 Antigravity 并登录，然后在程序中点击“添加当前账号”。源码运行时默认数据目录为仓库下的 `Data/`；本机旧配置 `local-data/` 仍可兼容读取。请勿把个人凭据、配置、日志或缓存提交到公开仓库。

## 配置和额度查询

示例配置见 [`account-switcher.config.example.json`](account-switcher.config.example.json)。配置中的相对路径以便携根目录（即 EXE 所在目录）为基准；需要保存到 Data 的配置路径会明确包含 `Data/`。可在界面顶部选择“代理设置”，输入本地 HTTP 代理端口（例如 `10809`，会保存为 `http://127.0.0.1:10809`），留空则直连；也接受完整 HTTP 代理地址。查询进行时代理设置会暂时禁用。通过界面保存的设置在下一次额度刷新时生效；手动编辑配置文件中的 `proxy` 字段需要重启程序。

启用 `queryQuota` 时，程序启动后会在后台查询额度；也可手动刷新。每个账号的查询预算为 45 秒，用于限制后续尝试及请求 socket timeout；DNS 解析或单次阻塞读取可能超过预算，因此不保证硬实时。查询失败时界面会显示状态。`--quota-only` 会发起真实网络请求，只在明确需要时手动运行。

## 隐私与安全

账号凭据含有敏感认证数据。Data 中的配置、凭据、日志和缓存均以明文保存在本机。日志会过滤常见 token 字段，但排障前仍应检查并遮蔽个人信息。不要发布 Data、个人配置、日志、缓存或安全扫描报告，也不要暴露 token、邮箱等个人数据。安全问题请参阅 [SECURITY.md](SECURITY.md)。

## 开发与发布

运行测试需要 Python 和 pytest：

```powershell
python -m pip install -r requirements-build.txt pytest
python -m pytest -q
```

本机构建 Windows x64 便携版需要 64 位 CPython 3.14.x；在 Windows 仓库根目录运行：

```powershell
python -m pip install -r requirements-build.txt
python packaging/build_portable.py
```

该构建会在 `dist/` 生成便携 ZIP、源码 ZIP 和 SHA-256 清单；源码包名称为 `AntigravityAccountSwitcherPortable-0.1.0-source.zip`。普通源码运行仍支持 Python 3.10 或更新版本。

本项目自有 Python 源码按 MIT License 发布，第三方运行时和依赖遵循各自许可证，详见 [LICENSE](LICENSE) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。`reference` 中曾包含外部 Antigravity Electron 应用的参考片段，不是本项目原创代码，也不是运行依赖；该目录不随公开源码或二进制发布。首次建立公开仓库时，应从经过筛选的源码 ZIP 初始化干净仓库，不要直接推送包含旧 reference 的本地开发 Git 历史。Git 历史不能由本项目文档清除。

GitHub Actions 的 `portable-release` workflow 可在 Fork 的默认分支通过 `workflow_dispatch` 手动打包，生成 `AntigravityAccountSwitcherPortable-0.1.0-win10-x64-portable.zip` 和 `AntigravityAccountSwitcherPortable-0.1.0-source.zip`。`ci.yml` 仅在 main 分支推送和 Pull Request 时运行测试。维护者推送与 `VERSION` 一致的 `v0.1.0` 标签后，`portable-release` 会构建并创建草稿 Release，最终由维护者人工发布。项目不提供自动更新，也没有代码签名；请从项目 GitHub Releases 获取发布包并核对发布说明。
