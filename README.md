# SaltLRCStudio v1.0.1

SaltLRCStudio 是一款面向 **Salt Player for Windows（椒盐音乐）** 的本机歌词工作台。它可以读取播放器曲库，从多个歌词来源获取原文与翻译，在音频波形上校准逐行或逐字时间轴，并导出 LRC / YRC 歌词。

除联网搜索歌词和调用用户自行配置的 AI 翻译服务外，歌词编辑、波形分析、智能贴唱、工程保存与导出都在本机完成。

## 主要功能

- 支持网易云音乐、LRCLIB、QQ 音乐、酷狗音乐和酷我音乐五个歌词来源
- 网易云登录后可获取真实逐字时间轴与翻译；其他来源可按字符权重生成参考逐字时间
- 波形编辑支持整句拖动、句首句尾微调、逐字拖动、Alt 联动后续字及毫秒级键盘微调
- 内置本机“智能贴唱”，分析人声起点并生成可预览、可撤销的整曲对齐结果
- 支持兼容 OpenAI `/chat/completions` 协议的翻译服务，包括 OpenAI、DeepSeek、智谱、通义以及本机 Ollama / LM Studio
- 支持 `.saltproj` 工程文件，可选择内嵌音频，方便跨设备继续编辑
- 支持 LRC / YRC 导出、直接写入歌曲目录和批量导出 ZIP
- 提供错误详情、操作追踪、运行日志与诊断包，方便排查问题

## 下载与安装

前往 [Releases](../../releases/latest) 下载：

`SaltLRCStudio-Setup-1.0.1.exe`

安装器按当前用户安装，不要求管理员权限。卸载程序不会删除 `%LOCALAPPDATA%\SaltLRCStudio\` 下的登录状态、设置、日志，也不会删除已经保存的工程和歌词。

> 当前安装包未使用商业代码签名证书。Windows 可能显示 SmartScreen 提示，请下载后先核对发布页提供的 SHA-256。

## 基本使用流程

1. 启动 SaltLRCStudio，点击“从曲库导入”选择歌曲，也可以直接拖入歌词、音频或 `.saltproj` 工程。
2. 选择歌词来源并获取歌词；需要真实逐字时间轴时可登录网易云音乐。
3. 在波形编辑器中调整整句、句首句尾或逐字时间，也可以使用“智能贴唱”生成预览后再应用。
4. 使用“导出歌词…”保存 LRC / YRC，或保存 `.saltproj` 工程以便稍后继续。

## 数据与隐私

- 歌词编辑、音频分析和工程处理在本机完成。
- 网易云登录凭据只保存在 `%LOCALAPPDATA%\SaltLRCStudio\credentials.json`。
- AI 翻译配置与 API Key 只保存在 `%LOCALAPPDATA%\SaltLRCStudio\settings.json`。
- 界面不使用远程 JavaScript 或 CDN。
- 请勿分享凭据和设置文件；在共用电脑上使用后应退出登录并清除 API Key。

## 支持的输出

- LRC / YRC 歌词
- 主歌词逐字时间轴与翻译整行
- `.saltproj` 工程文件，可选内嵌音频
- 批量歌词 ZIP

## 系统与兼容性

- 目标平台：Windows 10 / 11（64 位）
- 配套播放器：Salt Player for Windows
- 个别无法由内置组件解码的音频格式（例如 `.ape`、`.wv`）建议先转换为 `.mp3` 或 `.flac`

## 完整性校验

发布页同时提供 `SHA256SUMS.txt`。在 PowerShell 中可以这样核对：

```powershell
Get-FileHash .\SaltLRCStudio-Setup-1.0.1.exe -Algorithm SHA256
```

v1.0.1 安装包的 SHA-256：

```text
45d3cc22b85d6b54ad4ea9552b6eee969a8d3571cd49b1be92f299d13137062e
```

## 第三方组件

软件使用的第三方组件及许可信息随安装包一并提供，安装后可查看 `THIRD_PARTY_NOTICES.md`。
