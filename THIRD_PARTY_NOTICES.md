# 第三方软件与许可证说明

本项目自有 Python 源码使用 MIT License，见 [LICENSE](LICENSE)。发布包还包含第三方运行组件；它们不属于本项目自有源码，并按各自许可证提供。

每个发布包的 `Other/` 由构建流程从实际构建环境采集并包含自有 `LICENSE`，以及 Python、Tcl/Tk 和 PyInstaller 的真实许可证文本（`Python-LICENSE.txt`、`TclTk-LICENSE.txt`、`PyInstaller-COPYING.txt`）。PyInstaller 文件包含 bootloader exception。维护者需在每次发布前核对构建产物中的文本与实际打包组件一致：

- Python 运行时及随附标准库组件按其发行包许可证提供；Tcl/Tk 按运行时附带许可证提供。
- PyInstaller 按其许可证提供，bootloader exception 包含在 `PyInstaller-COPYING.txt` 中。
- 如构建纳入其他第三方组件，也须将其对应许可证和通知放入 `Other/`。

上述文件以该次构建实际打包内容为准，不应把概括性说明视作完整的第三方许可证文本。
