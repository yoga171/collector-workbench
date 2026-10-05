# 采集工作台 · Collector Workbench

一个本地运行的采集与下载工作台，集中管理数据采集、店铺与竞品、分享链接视频及任务记录。

## Windows 用户下载使用

1. 在 [Releases](https://github.com/yoga171/collector-workbench/releases/latest) 下载 **`CollectorWorkbench-0.3.0-windows-x64.zip`**。
2. 解压整个压缩包到一个可写的目录，例如桌面或文档目录。
3. 双击 **`Start-Workbench.cmd`**，默认浏览器会打开工作台。
4. 选择采集方式，先检查采集条件，再开始任务。

便携版面向 **Windows 10/11、64 位 Intel/AMD**，附带 Python、Node、ADB 与浏览器组件，无需安装 Python 或管理员服务。首次启动不安装依赖；访问平台内容仍需要联网。不要在压缩包内直接运行，也不要只提取一个启动文件。

GitHub 绿色 **Code → Download ZIP** 下载的是源码，运行环境不在源码包内。普通用户请下载 Releases 中的 Windows 便携包。

## 功能

- 手机 APP、浏览器和已保存网页采集，按方式检查设备、目标 APP、组件、文件和磁盘空间。
- 后台任务队列，显示实际阶段，支持取消、重试、恢复参数；刷新页面后可继续查看。
- 同一手机按顺序执行，取消会停止任务及子进程，保留已有文件。
- 店铺与竞品模块、MediaCrawler，以及抖音视频/图集解析与下载入口。
- 按来源、关键词/平台、状态和日期筛选任务，统一预览与 Excel 等格式下载。
- 定时任务、失败记录和服务中断后的任务恢复。

专用店铺与竞品采集及浏览器视频捕获沿用模块原有执行流程，记录和文件集中查看。

## 不登录也能验证启动

打开“数据采集”，选择“公开网页”和“已保存网页”，输入 `examples/local-demo.html`，关闭评论，再执行采集。这个本地样例可生成 Excel，便于确认软件在本机正常运行。

## 手机与账号准备

- 手机采集需安卓手机、USB 调试授权及对应 APP；便携包提供 ADB，不包含安卓模拟器和平台 APP。
- 各平台由用户登录自己的账号。浏览器或 APP 出现验证时需自行处理。
- 分享链接视频解析受平台接口、链接可访问性及登录状态影响，软件启动成功不代表每个链接必然成功。
- 视频模块需要自己的 Cookie 时，双击 `Start-Video-Login.cmd`，在原视频模块的配置页面完成登录。
- 浏览器播放捕获需在 Chrome/Edge 的扩展页面手动加载 `tools/VideoDownloadHelper-windows-v0.2.4-20260706-141912/video-download-helper-extension`。此包未自动安装浏览器扩展。
- 原店铺模块的视觉模型增强功能需要另行配置 Ollama 和模型，便携包不包含模型文件。

## 数据位置与退出

数据保存在解压目录内：`data/`、各模块的 `cookies/`、下载目录及浏览器登录目录。发行包以空数据状态启动，不包含维护者的账号或历史记录。

关闭浏览器不会结束后台任务。双击 **`Stop-Workbench.cmd`** 停止本包启动的服务。升级前先退出软件，保留自己的数据和登录目录，再将它们迁入新版本。正在运行的 SQLite 数据库应使用 SQLite 备份方法，或退出后复制。

默认地址为 `http://127.0.0.1:8765/`；端口被占用时会选择其他本地端口，启动器会打开正确地址。所有自动启动的服务仅监听本机地址。

## 从源码开发

主工作台使用 Python 3.11。安装依赖并从项目根目录运行：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-main.lock.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps
.venv\Scripts\python.exe -m social_crawler.cli app
```

模块使用各自的运行环境。`scripts/build_portable.py` 根据已有的构建环境生成允许列表内的干净源码快照、运行目录和 ZIP；它不会复制数据库、Cookie、历史数据、Git 目录或旧备份。

```powershell
.venv\Scripts\python.exe scripts\build_portable.py --stage source
.venv\Scripts\python.exe scripts\build_portable.py --stage runtime
.venv\Scripts\python.exe scripts\build_portable.py --stage zip
```

依赖及模块环境见 `requirements-main.lock.txt`、`requirements-video.lock.txt`、`requirements-media.lock.txt` 和 `requirements-helper.lock.txt`。

## 许可

工作台原创整合代码采用 MIT。第三方代码保留各自许可，详见 [第三方来源与许可](THIRD_PARTY_NOTICES.md)。本包包含 **仅限非商业学习与研究** 的 MediaCrawler 组件，不应把整包视为可自由商业使用的软件。
