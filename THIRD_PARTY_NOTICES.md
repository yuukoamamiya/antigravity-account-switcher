# 第三方软件与许可证说明

本项目自有 Python 源码使用 MIT License，见 [LICENSE](LICENSE)。发布包还包含第三方运行组件；它们不属于本项目自有源码，并按各自许可证提供。

构建发布包时，维护者应根据该次构建实际打包内容更新下列清单，并将对应许可证文本放入 `Other/`：

- Python 运行时及随附标准库组件：按 Python Software Foundation License 和各组件随附许可文件。
- Tcl/Tk 运行时（若随构建分发）：按其发行包附带的许可证。
- PyInstaller：按其许可证分发；PyInstaller bootloader 适用 bootloader exception。
- 实际纳入包中的其他依赖或组件：按各自发行包中提供的许可证和通知。

每个发布包的 `Other/` 应包含与该次构建对应的完整第三方许可文本。打包清单须以构建产物实际内容核实，不应将上述概括视为完整的发布依赖清单。
