# 第三方软件与许可证说明

本项目自有 Python 源码使用 MIT License，见 [LICENSE](LICENSE)。发布包还包含第三方运行组件；它们不属于本项目自有源码，并按各自许可证提供。

每个发布包的 `Other/` 由构建流程从实际构建环境采集并包含本项目 `LICENSE`，以及 Python、Tcl/Tk 和 PyInstaller 的许可证与通知文本：`Python-LICENSE.txt`、`Python-Third-Party-Licenses.rst`、`Tcl-LICENSE.txt`、`Tk-LICENSE.txt`、`PyInstaller-COPYING.txt` 和 `Apache-2.0.txt`。PyInstaller 文件包含 bootloader exception；Apache-2.0.txt 覆盖嵌入的 PyInstaller runtime hooks。维护者需在每次发布前核对构建产物中的文本与实际打包组件一致：

- Python 运行时及随附标准库组件按其发行包许可证提供；Tcl/Tk 按运行时附带许可证提供。
- PyInstaller 按其许可证提供，bootloader exception 包含在 `PyInstaller-COPYING.txt` 中。
- 如构建纳入其他第三方组件，也须将其对应许可证和通知放入 `Other/`。

上述文件以该次构建实际打包内容为准，不应把概括性说明视作完整的第三方许可证文本。

## 上游项目参考

开发过程中参考了 [lbjlaq/Antigravity-Manager](https://github.com/lbjlaq/Antigravity-Manager) 的产品功能与行为。审查本仓库当前文件和可见 Git 历史时，没有识别出从该项目直接复制或改编的代码；本项目使用独立的 Python/Tkinter 实现，而该上游项目使用 Tauri、Rust 和 React。该项目的许可证为 [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)（[上游 LICENSE](https://github.com/lbjlaq/Antigravity-Manager/blob/main/LICENSE)）。这项参考说明不表示上游代码纳入本项目，也不把上游许可证延伸到本项目；本项目原创源码仍按仓库根目录的 MIT [LICENSE](LICENSE) 发布。

早期 Git 历史还包含 `reference/antigravity-electron/` 下的外部 Electron 应用参考片段。这些文件不是本项目运行依赖，也不随发布包分发。它们没有附带可核实的来源许可证或版权声明；如需重新分发或改编这些片段，应先确认原始来源及许可。
