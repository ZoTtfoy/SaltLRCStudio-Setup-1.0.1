# SaltLRC Studio v1.0.5

给 **Salt Player for Windows（椒盐音乐 / SPW）** 配歌词的本机工具。

读取播放器曲库 → 联网取回原文与翻译 → 在波形上对时间 → 导出 LRC / YRC 写到歌曲旁边。
界面是本机的，数据不上传；除「联网找歌词」与「AI 翻译」外全程离线。


## 能做什么

| 能力 | 说明 |
|---|---|
| 五个歌词来源 | 网易云音乐、LRCLIB、QQ 音乐、酷狗音乐、酷我音乐；可随时切换 |
| 逐字时间轴 | 网易云登录后取真实逐字（YRC）；其它来源按字符权重估算 |
| AI 翻译 | 兼容 OpenAI `/chat/completions` 的任何服务；可只补缺失行 |
| 波形编辑 | 整句拖动、句首句尾微调、逐字拖动（Alt 拖动推开后续字） |
| 逐字编辑器 | 自动分词、打点模式、按字数均分、从音频检测这一行 |
| 智能贴唱 | 本机分析人声起点、整首单调路径对齐，先预览再应用，可撤销 |
| 曲库联动 | 直接读 Salt Player 曲库、按曲库写入歌词、批量匹配 |
| 工程文件 | `.saltproj` 记录歌词 / 逐字 / 翻译 / 歌名 / 时长，可内嵌音频 |
| 批量导出 | 队列中通过检查的歌打成 ZIP |
| 诊断 | 出错弹框（可复制详情）、后台失败横幅、Ctrl+Shift+T 操作追踪 |

详细的界面操作见 `使用说明.txt`。


## 运行与打包

```powershell
# 从源码运行（打开独立窗口）
python app_entry.py

# 打包成单文件 exe（产物：dist\SaltLRCStudio.exe）
powershell -ExecutionPolicy Bypass -File packaging\build.ps1

# 一条命令完成发布：版本同步 → 测试 → 打包 → 冒烟 → 建交付目录 → 校验
powershell -ExecutionPolicy Bypass -File packaging\release.ps1
powershell -ExecutionPolicy Bypass -File packaging\release.ps1 -MakeInstaller   # 顺带编译安装包
```


## 目录结构

代码按四层组织，**底层不依赖上层**：

| 层 | 目录 / 文件 | 职责 |
|---|---|---|
| L0 | `saltlrc/document.py`、`saltlrc/web/document.js` | 文档模型与时间轴不变式（两侧规则一致） |
| L1 | `saltlrc/parser.py`、`exporter.py`、`validator.py`、`edits.py`、`wordtime.py`、`tokenizers/`、`fit.py`、`acoustic.py`、`alignment.py` | 解析、导出、校验、结构编辑、分词、贴唱与对齐 |
| L2 | `saltlrc/providers.py`、`sources/`、`library.py`、`projects.py`、`folders.py`、`store.py`、`credentials.py`、`settings.py`、`window.py`、`diagnostics.py`、`trace.py`、`crypto.py`、`audioformats.py`、`assoc.py` | 外部世界：歌词源、播放器曲库、本地文件与设置、窗口、日志 |
| L3 | `saltlrc/server.py`、`saltlrc/api/` | 本机 HTTP 服务与路由（按域分组） |
| L4 | `saltlrc/web/` | 界面：`index.html` / `library.html`、`studio.js`、`waveform.js`、`style.css` 等 |

```
app_entry.py            入口：解析参数 → 开窗口或起服务（含 --open 打开指定工程）
saltlrc/                应用代码（L0–L3）
saltlrc/web/            界面（L4）
tests/                  单元测试与前端检查（含 jsdom）、基准与反向验证脚本
docs/                   文档
packaging/              打包脚本（build.ps1 / release.ps1 / smoke-exe.ps1 / installer.iss）、图标
```


## 依赖

**运行时：纯标准库。** 网易云登录用的加密信封由 `saltlrc/crypto.py` 实现（纯 Python AES-128，
已用 FIPS-197 向量验证），二维码由 `saltlrc/qrcode.py` 生成（含手写 PNG 编码），
不引入 pycryptodome / Pillow / qrcode。

打包与界面依赖（仅开发/打包时需要）：

- `pywebview` + `pythonnet`：独立窗口（WebView2 宿主）
- `pyinstaller`：打包
- `Pillow`：在测试里校验二维码能被真实解码器读出

可选：语音识别对齐（`faster-whisper` / `HeartTranscriptor`，跑 `setup-ai.ps1` 安装）。
不装也能用 —— 「智能贴唱」用纯本机能耗分析。


## 测试

```powershell
python -m unittest discover -s tests -p "test_*.py" -t tests
```

`-t tests` 必须有（测试互相 import 兄弟模块）。测试同时覆盖 Python 侧与界面侧
（jsdom 加载真实页面并断言控件接线）。

辅助脚本：

```powershell
python tests\_bench_io.py          # 打开 / 编辑 / 保存链路的基准
python tests\_reverse_checks.py    # 反向验证：把改动改回旧写法，断言测试会失败
```

`packaging\smoke-exe.ps1` 对打包好的 exe 做一次冒烟（起服务、跑页面检查、退出）。
`release.ps1` 会依次跑测试、打包、冒烟、建交付目录，并校验复制前后哈希一致。


## 数据与隐私

- 歌词处理全部在本机完成；搜索与歌词请求只发往你选择的那家来源。
- 登录凭据（网易云）与 AI 翻译的 Key 只写 `%LOCALAPPDATA%\SaltLRCStudio\`，
  接口只回状态、不回内容。
- 界面无远程 JS / CDN，不加载任何外部资源。
- 文件关联（`.saltproj`）只写当前用户注册表，可在设置里取消。


## 许可

`THIRD_PARTY_NOTICES.md` 列出第三方组件与许可。歌词内容版权归各来源与权利人所有。
