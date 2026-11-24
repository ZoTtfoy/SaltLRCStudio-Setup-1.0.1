# 分层结构（从哪里开始改）

这份文件是**修改记录的一部分**：先说清楚每一层负责什么、动一个功能该碰哪些文件，
再按「症状 → 该看哪里」给一张定位表。修 bug 前先读这里，能少走很多弯路。

## 一句话原则

**底层只做一件事，且不允许依赖上层。** 文档模型不知道有 HTTP，编辑操作不知道有浏览器，
浏览器不知道有 PyInstaller。越往下越纯，越好测。

## 分层

| 层 | 职责 | Python | 浏览器 |
|---|---|---|---|
| **L0 文档模型** | 歌词工程是什么：Word / Track / Group / Document，以及**永远不能被破坏的时间轴不变式** | `saltlrc/document.py`（`models.py` 只是兼容垫片） | `saltlrc/web/document.js` |
| **L1 编辑与加工** | 改变工程形状（插行/删行/排序）与生成内容（解析、逐字、对齐、校验、导出），以及**交互追踪** | `edits.py` `parser.py` `wordtime.py` `tokenizers/` `bilingual.py` `acoustic.py` `alignment.py` **`fit.py`** `validator.py` `exporter.py` `repair.py` `language.py` `batch.py` `trace.py` | — |
| **L2 外部世界** | 曲库、网络、文件系统、进程 | `library.py` `providers.py` `crypto.py` `inspect.py` `folders.py` **`projects.py`** `store.py` `settings.py` `credentials.py` `jobs.py` `worker.py` `transcriber.py` `diagnostics.py` **`sources/`**（5 家歌词源 + 共享 `_http.py`） | `web/projects.js`（工程库的界面侧） |
| **L3 传输** | 把 L1/L2 暴露成本机 HTTP；静态资源路由 | `server.py`（传输 + 静态白名单）、**`api/`**（按域分组的路由，由 `api.dispatch` 派发）、`app_entry.py`、`__main__.py` | `fetch`（`api()` 在 studio.js / library.js） |
| **L4 界面** | 波形、表格、对话框、历史 | — | `web/studio.js` **`history.js`** `waveform.js` `splitter.js` `library.js` `common.js` `audio-tools.js` **`theme.js`** `debug-panel.js` `trace.js` |

### L2：本地工程库（`projects.py` ↔ `api/projects.py` ↔ `web/projects.js`）

每导入一个音频/歌词文件就在**用户指定的目录**里建一个工程，退出时由用户决定留不留。

```
%LOCALAPPDATA%\SaltLRCStudio\project-root.json     保存路径（用户选的那个目录）
%LOCALAPPDATA%\SaltLRCStudio\session-projects.json 本次会话建过的工程
<保存路径>\20260926-081207-歌名\project.saltproj   工程本体（.saltproj 格式，可被「打开工程」读回）
```

| 路由 | 用途 |
|---|---|
| `POST /api/projects/config` | 读保存路径 + 本次会话的工程列表（启动时一次） |
| `POST /api/projects/root` | 设保存路径；`{"pick":true}` 走系统文件夹选择框 |
| `POST /api/projects/create` | 建一个工程目录（**目录名由本模块生成**） |
| `POST /api/projects/write` `/read` | 写 / 读工程正文（原子写） |
| `POST /api/projects/keep` `/discard` | 保留（只清记录，文件不动）/ 删除本次会话建的 |

**唯一权威**（这个项目里"同一件事两处各算一遍"修过 6 次以上）：

| 规则 | 唯一权威 |
|---|---|
| 会不会建工程 | `projects.root()` —— 没有保存路径就不建，也不假装建了 |
| 什么时候写盘 | `history.js::persist()`（每次改动的唯一出口）→ `Project.sync()` |
| 谁变过 | **歌词指纹**（`JSON.stringify(item.document)`），不看"谁是当前选中的歌" |
| 该删哪些 | `projects.discard_session()`；页面不拼路径、不删文件 |
| "本次会话" | `serve()` 启动时 `projects.start_session()`：**只清记录，不删文件** |

**删除的边界**（不可逆，所以写死在 L2，不交给调用方）：工程目录必须在保存路径
**之内**（`_inside` 比 `realpath`）才允许删；**保存路径本身永不删除**；换过保存路径
之后旧工程拒绝删除（宁可不删，也不越界）；开新会话只清记录。`ProjectError`
继承 `ValueError`，这样 `server.do_POST` 会把它转成 400 + 原文（用户看得懂），
而不是一句 500。

### L1：交互追踪（`trace.py` ↔ `web/trace.js`）

**"点了没反应"是一个可以机器判定的事实，不是只能靠描述的症状。**
记录留在 **L1（Python 进程）**而不是只留在浏览器里，这样它跨刷新存活、
不用浏览器就能分析、还能整份导出给报告用。

- **记三件事，对应三种提问**：

  | 用户的提问 | 记录的东西 |
  |---|---|
  | 我干了什么 | `gesture`（捕获阶段的 click / keydown / change）+ 应用主动上报的 `dialogOpen` |
  | 软件返回了什么 | `api-start` / `api-end`（含**状态码与耗时**）/ `error` |
  | 在哪里卡住 | `analyse()`：**手势之后、下一个手势之前没有任何可观察结果 → `stalled`**；迟到的结果记 `late_ms` |

- **`analyse()` 是纯函数**（`events + stall_ms → 结论`，不碰全局状态），
  所以判定规则可以被单测直接钉住，不依赖浏览器。
- **窗口宽度只有一个权威**：服务端 `STALL_MS = 900` 与客户端
  `trace.js` 的 `WINDOW_MS = 900` 必须一致，否则"页面提示没反应"与
  "报告判为没反应"会给出两种结论。
- **`record()` 永不抛异常**：诊断通路坏掉不能连带把被测功能弄坏。
- **环形缓冲**：`LIMIT = 4000`；`total` 用独立计数器累加，
  于是**"仍在缓冲里的（retained）"与"累计发生的（total）"不会混淆**。
- **`Ctrl+Shift+T` 刻意不可改键**：键位配置出问题时，
  诊断通路必须仍然可用（`settings` 不接管它）。
- **`bind()` 接不上控件时写日志**：这是让"控件没接线"这类缺陷
  **自己浮出来**的机制，而不是等人去读 HTML。
- 入口：`/api/trace`（上报或读取）、`/api/trace/clear`；`STATIC` 要登记 `trace.js`。
- 回归：`tests/test_trace.py`（13 项，纯函数部分）+ `tests/web_library_word_check.js`（真页面）。

### L1：分词按语言分模块（`tokenizers/`）

**"什么算一个单元"是语言问题，不是排版问题** —— 逐字打点的每个单元就是
一次打点对应的东西：

| 语言 | 模块 | 规则 |
|---|---|---|
| 中文 | `chinese.py` | 按字（一个汉字一个单元） |
| 日语 | `japanese.py` | 按发音（漢字+送りがな 成一组；假名按可唱长度 ≤5 整 / 否则每 3） |
| 英语 | `english.py` | 按单词（撇号属于单词） |
| 韩语 | `korean.py` | 按音节 |
| 未识别 | `mixed.py` | **委托**给 `japanese.regroup`，并如实标注是猜的 |

- **共用第一趟扫描**（`common.first_pass`）：拉丁整词、其余逐字。
  语言模块只做"在这之上的合并"，因此**"逐字符还原原文"这条不变量天然成立**
  （导出与校验都依赖它；当年丢空格曾让英文歌整首导不出来）。
- **合空格只有一处**（`common.merge_spaces`）：空格永不单独成单元，
  附着到相邻单元上。
- **兜底不复制规则**：`mixed` 直接调用 `japanese.regroup` ——
  两份同样的规则迟早漂移。测试钉住"两者同源"。
- **语言来源**：调用方的 `lang` 提示优先（整首歌的语言 > 一行短文本），
  否则逐行 `language.detect`。纯汉字行（`東京`）光看脚本分不出中日，
  **这正是"提示优先"存在的理由**。
- `char` / `word` 两种粒度**与语言无关**：要求"一个字一格"的人，
  要的就是每个字一格，不分语言。

回归：`tests/test_tokenizers.py`。

### 编辑模型：一套规则，一个权威（v0.12.3）

时间轴上有四类可拖的东西：**句首**、**逐字**、**句尾**、**整句（块体）**。
它们看起来是四种交互，其实是**同一条规则**：

```
任一刻度的合法范围 = (前一个刻度, 后一个刻度)
句首另外还要受相邻两行的限制；句尾的上限是音频时长。
越界 → 就地钳制 + 说明被谁挡住（绝不整笔驳回）
Alt  → 连同后面的刻度一起走（保持间距）
```

这条规则定义在**一处**：`Doc.tickRange(doc, index, field, kind, word, duration)`
返回 `{low, high}`，`changeTime` 的每个分支都向它要范围，绝不自己算。

**必须只有一个入口**：每条路径各自赋值、再让
`validateEdit` 判，结果都一样：**越过邻居 → 整笔编辑被丢弃 → 控件纹丝不动**，
而提示说的是一条用户看不见的约束。

| 版本 | 坏掉的控件 | 症状 |
|---|---|---|
| v0.11.14 | 最后一个字（句尾） | 字拖不动，"卡在尾部" |
| v0.12.2 | 中间的字（下一个字） | 往右拖没反应，"歌词分不开" |
| v0.12.3 | **句首 / 句尾本身** | **"整句歌词时间轴都没办法改"** |

**"整笔驳回"是错的交互模型**：校验器是**不变量**的守卫，不是**手势**的解释者。
手势的可接受范围必须由图层面先算出来并钳制；校验器只用来保证结果合法。
新增任何编辑路径时，先问：**它的合法范围由谁算？** 答案只能是 `tickRange`。

配套的纯函数（都在 L0，可脱离界面测试）：

- `moveWordStart` / `rippleWordStart`：逐字（同一规则的轨道内应用）。
- `moveLineEnd`：句尾（含 Alt 时把越界的字一起前移，且**永不带走第一个字** ——
  它的起点就是句首，那是句首把手的职责）。
- `fitLineEnd`：对话框里手填句尾时的兜底。

### L4：主题（`settings.darkMode` ↔ `web/theme.js` ↔ `style.css`）

**颜色只有一个权威：`style.css`。**

- 两套令牌写在同一个文件里：`:root` 是深色（默认），`html[data-theme="light"]` 是浅色。
  每个颜色令牌都必须有浅色取值（`tests/test_theme.py` 钉住），**别处一律不许再写颜色字面量**
  （同一条测试扫全表）。
- `theme.js` 是页面里**唯一**写 `documentElement.dataset.theme` 的模块，同时负责
  按主题读 CSS 令牌给画布用：`Theme.token('--wave-bg')` 读一次缓存，换主题时整份作废
  （画布每帧要取十几个颜色，不能每帧问 `getComputedStyle`）。
- 首屏：两页 `<head>` 里有一小段内联脚本，按 `localStorage` 里上次的选择先定下主题。
  那份缓存**只负责"别闪一下"**，权威始终是 `settings.json`：两页拿到设置之后都会
  用 `Theme.apply()` 校准回来。
- 换主题必须让缓存的图层重画：波形静态层的判据里**含主题**（`staticKeyMatches`），
  所以换主题后第一次绘制必然重画；`studio.js` 另挂一个 `themechange` 监听让它当场就变。
- 回归：`tests/test_theme.py`（14 项，含对比度与字格色差）、`tests/web_theme_check.js`
  （11 项，含"换主题后静态层必须重画"）、`tests/web_dom_check.js` 里的开关接线。

**固定深色的三处**：`debug-panel.js`、`trace.js`（诊断浮层）与 `debug.html`
（带口令的内部诊断页）。

### L0：文档模型（`document.py` ↔ `web/document.js`）

**边界靠"就地处理"，不靠"事后驳回"。** `validateEdit` / `document.check` 是
**规则的唯一权威**；界面侧的辅助函数不复制规则，而是**保证产物能通过它**：

- `moveWordStart(track, word, time, {duration, lineStart})`：拖一个逐字刻度。
  夹住"不越过前一个字"；**最后一个字带着句尾一起走并保留原有尾距**
  （句尾不可能早于最后一个字，所以往后拖它**语义上就包含**"这句结束得更晚"）；
  真正卡住拖动的是「音频末尾 − 尾距」，否则句尾会被顶出音频。
- `fitLineEnd(end, words, duration)`：把用户填的句尾抬到合法值（最后字 + 1）。

**不能事后驳回**：`ticks()` 把句尾放在序列最后且要求严格递增，
于是"越过句尾的一拖"会让**整笔编辑非法被驳回**，提示还是与边界无关的
「轨道时间必须递增且在音频范围内」——用户只看到"这个字拖不动"。
**用户能看见的边界，必须能用手越过或明确挡住，不能表现为操作没反应。**

回归：`tests/web_document_check.js`（产物一律喂给真正的 `validateEdit`）。

两侧**同名同规则**，是最容易漂移的一层，所以两边跑同一组夹具：

- Python：`tests/test_edits.py`
- 浏览器：`tests/web_document_check.js`

夹具完全一致（三行 `[1000, 3000, 9000]`、时长 `12000`），任一侧改了规则另一侧立刻红。
**改任何一条时间规则，两边都要改，两边都要跑。**

不变式（`check()` / `Doc.validateEdit()`）：

1. 句首是非负整数、严格递增且唯一；
2. 每条轨道的时间序列（句首 → 各字 → 句尾）非负、递增、不超出音频时长；
3. 首个逐字时间必须等于轨道开始时间。

### L1：编辑（`edits.py`）

结构性编辑只有三个：`insert_group` / `delete_group` / `sort_groups`。
全部「先算时间 → 再校验 → 才落盘」，**绝不发明时间**：

- 插不进去（上下两行间隔 < 2ms）→ 抛错并提示先把两行拉开；
- 末尾插行超出音频 → 夹到音频末尾；上一行已经贴着末尾 → 明确拒绝。

结构性编辑走 `/api/edit` 由后端执行（浏览器本地也有一份镜像用于即时反馈）。

### L4：播放循环的所有权（`studio.js`）

**不变量：逐帧更新循环只在"音频正在播放"时运行。**

这条判断只能有一处 —— `syncPlaybackLoop()`，由 `play` / `pause` / `seeked` /
`ended` 四个事件共同调用。**判据是元素当前的状态（`!paused && !ended`），
不是"哪个事件发生了"。**

`seeked` 与 `pause` 分开处理：那个处理器会取消循环。
**跳转在播放中随时会发生**（点一下波形图就是一次跳转），于是播放中一次点击
就永久停掉了循环 —— `play` 只在播放**开始**时触发，不会来救。
症状是"声音还在放，画面完全静止"；因为暂停再继续能恢复，长期被当成偶发抖动。

> 推广到一般：**处理器的输入应当是状态，不是事件。** 事件只是"该重新看一眼状态"
> 的提示；把事件本身当成指令，就会把只在某个场景成立的动作误用到别的场景。

相关回归：`tests/web_playback_seek_check.js`（播放 → 点击 → 跳转 → 仍在渲染）。

### L4：原生控件与主题（`style.css` / `index.html`）

**规矩：原生控件要么跟随主题，要么自己画。**

浏览器绘制的控件（`<audio controls>` 的媒体条、`<input type=file>`、
原生滚动条、焦点圈）**不读我们的背景/颜色 CSS**，它们读的是 `color-scheme`。
`:root` 声明了 `color-scheme:dark`，所以这些控件按深色绘制。

`<audio id="audio">` **刻意保留但隐藏**（不带 `controls`）：
- 去掉 `controls` 是因为那条原生媒体条在深色界面里是一条亮带
  （`#f1f3f4`，40 CSS px 高、整个窗格宽）——它是浏览器画的，
  我们的样式改不动它；
- 保留元素是因为**全部播放都走它**（`play/pause/currentTime`）。

走带 UI 是自己的：`#playPause` + `#playTime` + `#volume`（音量滑块与百分比读数），
逻辑收敛在 `togglePlay()` 与 `applyVolume()`，
**空格键和按钮共用同一处**（否则两者的行为迟早不一致）。
音量由 `applyVolume()` 独占三件事 —— **元素音量、滑块位置、读数** ——
并持久化到 `localStorage['saltlrc-volume']`（夹到 0–100 后**存夹过之后的值**）。

回归：`tests/test_web_pages.py` 的
`test_the_native_media_bar_is_not_used` / `test_native_widgets_follow_the_dark_theme`；
`tests/web_dom_check.js` 真的推动滑块并断言 `<audio>.volume` 随之改变 ——
**只断言"控件存在且有处理器"抓不住"滑块能动但不改变任何东西"。**

### L4：表格跟随（`studio.js`）

**不变量：播放时当前行固定在列表顶部，每次换行归位一次。**

`pinRowToTop(row)` 把该行放到**粘性表头下方**（减掉表头高度），
由 `preview()` 在 `index !== shownRow`（跨行）时调用 ——
"每播放一行刷新一次"这个频率是天然成立的，不需要额外计时器。

两个容易做错的地方：

- **已经在该位置就不要动 `scrollTop`**（差值 < 1px 直接返回），
  否则会和用户自己的滚动打架；
- **只滚 `.tablewrap`**，不要用 `scrollIntoView()` ——
  后者会对齐所有可滚动祖先，历史上导致播放时整页上下跳。

这个行为挂在「跟随播放」开关下：关掉它，表格就停在使用者放的地方。

> 注意语义差别："别让当前行跑出视野"（贴边才滚）和"把当前行固定在顶部"
> 是**两种产品决策**。前者会让当前行漂到列表底部（73% 处、其下只剩 1 行），
> 而打轴时真正要看的是**下面几句**，所以后者才是对的。

回归：`tests/web_row_pin_check.js`（自建几何模型，**行的 rect 随 scrollTop 推算**）。

### L4：波形渲染的性能模型（`waveform.js`）

**一句话：除了播放头和高亮框，一切静态；"平移"和"换高亮"都不算内容变化。**

画布每次重绘只有两种成本：

| 动作 | 成本 |
|---|---|
| 静态层**重建**（波形包络 + 网格 + 两轨歌词块/刻度/文字） | 高，约 2100 次绘制调用，随可见行数与宽度增长 |
| 覆盖层（`drawImage` 贴图 + 播放头 + 高亮框） | 低，约 28 次绘制调用，与文档长度无关 |

所以规矩是：**任何"只是换个位置看"或"只是换个框"的操作都不许触发重建。**

- `paint()` 的缓存键**只含真正改变内容的量**：尺寸、DPR、`pps`、
  `docVersion`、`peaks` 数量。**`offset` 刻意不在其中。**
- **但"不在缓存键里"不等于"不用管"**：贴图方案还有一条前提 ——
  **视窗必须仍落在已绘制范围内**（`viewportIsCovered()`）。
  漏掉它，播放/滚动时视窗一旦走出范围就**再也不会重画**，
  画布只剩每帧的底色填充（看起来就是"波形消失、跟随失效"）。
  现象：播放 9 秒后开始空白，17 秒时整块画布全空，而 20 秒内只重建了 1 次。
  **凡"缓存 + 平移"的渲染，都必须有一条"缓存还覆盖当前视窗吗"的判断。**
- 静态层画在**粗锚点** `layerAnchor`（`snapAnchor()`，整秒对齐）上，
  贴图位移 `(layerAnchor - offset) * pps * ratio`。锚点整秒，
  因此普通滚动不会让它失效。
- 层宽是 `panSpan()`，**必须始终大于一个视窗**：层按整秒锚点绘制、再按
  `(anchor - offset)` 贴图，所以最多偏移半秒。层宽**正好等于**视窗时，
  任何非整数 `offset`（follow 模式下每帧都是）都会让画布边缘露出一条缝。
  v0.11.2–v0.11.4 就是这样留下一条黑带的：因为是**透明画布**在兜底
  （露出的是页面背景色，看不出来），v0.11.4 把画布改成不透明后，
  `clearRect` 留下纯黑，缝才现形。现在左右各留 1 秒余量
  （拖动时更多），并且画布底色改用波形底色兜底。
  **不变量：贴图必须覆盖整个画布**，由 `tests/web_layer_coverage_check.js`
  在 6 个缩放 × 211 个 offset 上直接断言。
  拖动时余量更大（≥2 秒/侧），所以一次重建能撑过很长一段拖动；
  拖动开始/结束各重建一次，**不是每帧**。
- **坐标陷阱**：层坐标 ≠ 画布坐标。`hits[].x` 记的是层坐标，
  命中判定必须补 `hitShift() = (layerAnchor - offset) * pps`。
  不补则画面正常但拖不动刻度（静默失效）。
- 上方贴条同构：`stripLayer` 缓存「时间尺 + 歌词块」，
  每帧只画「视窗框 + 播放头」。
- **高亮 / 悬停描边在覆盖层**（`paintHighlight()`），
  `setHighlight` / `setHover` **只 `draw()`，不 `invalidate()`**。
  它们曾经住在静态层里（注释还写着"这样每次换行只重画一次"），
  但那把"每次换行一次重建（2100 次调用）"换成了"每帧 4 次"——
  **优化错了坐标轴**。正确判据是 `变化频率 × 单次成本`：
  **高频低成本的覆盖物独立成层，低频高成本的内容才进缓存。**
- **行标题 / 轴标签同样在覆盖层**（`paintTrackHints()`）：
  它们是**行与轴的家具，不是内容**。画进缓存层就会被一起贴图平移 ——
  视窗一动就横移，甚至整条滑出画布（v0.11.12 之前就是这样）。
  判据与上面同源：**它会随什么变化？** 只随内容变化的东西才进缓存。
  回归断言写成**"屏幕坐标与 `offset` 无关"** ——
  "画了没有"在两种实现下都成立，抓不住这个问题。
- 提示文案里**不写语言**：主轨不一定是中文、翻译轨不一定是日语
  （v0.4 起 `require_language=False`）。

| 交互 | 之前 | 现在 |
|---|---|---|
| hover 一行 / 点击一行 / 换高亮 | 约 2150 次、每次重建 | 约 28 次、**0 次重建** |

- **宽度只有一个权威**（`canvasWidth()`）：`paint()` 用它布局画布，
  `viewportSeconds()`、`focusLine()`、`setTime()`、高亮裁剪也都用它。
  `measureWidth()` 是"窗格有多宽"，`canvasWidth()` 是"画布按多宽画"
  （含 200px 下限）—— **判断用的量和绘制用的量必须是同一个**。
  它们曾经不一致：窗格 150px 时画布按 200px 画，而覆盖判断以为视窗只有 150px 宽，
  于是判断通过、画布边上真的空着 20px。

**渲染用 Canvas 2D 而非 WebGL**：成本不在填充率（画布仅 900×292），而在每次交互提交
约 2100 次绘制调用。GPU API 只会让每次调用更快，不会让调用变少。
真正属于 GPU 侧的那一项已做：`getContext('2d', {alpha:false})` 让合成器跳过混合。
`desynchronized` 被**刻意排除**——它让画布走独立合成路径，
可能破坏与画布上方 DOM 浮层的层叠关系。

回归：`tests/web_drag_perf_check.js`（平移）、
`tests/web_interaction_perf_check.js`（交互，**同时断言成本与"真的画出来了"**）、
`tests/web_layer_coverage_check.js`（跟随播放 20 秒）、
`tests/web_cover_sweep_check.js`（**横扫每一种会移动视窗的交互，272 个采样点**）。
诊断工具：用代理按画布分别统计绘制调用次数 —— **桩里测不出光栅化耗时，
但调用次数是确定的**。

### L3：接口

| 路由 | 用途 |
|---|---|
| `POST /api/edit` | 结构性编辑：`op = insert / delete / sort` |
| `POST /api/parse` | 文本 → 文档 |
| `POST /api/build` | 文本 + 选项 → 逐字工程 |
| `POST /api/validate` | 导出前校验（返回 `items`，UI 可跳转定位） |
| `POST /api/export` `/api/zip` | 导出单曲 / 批量 |
| `POST /api/align/*` | 音频对齐（整首偏移、行内细化、候选应用） |
| `POST /api/export-folder` | 原生文件夹选择 + 后端写文件 |
| `POST /api/projects/*` | 本地工程库：`config` `root` `create` `write` `read` `keep` `discard`（见 L2 一节） |
| `GET /api/audio?track_id=` | 曲库音频（支持 Range） |

## 症状 → 该看哪里

| 症状 | 先看 | 通常还要看 |
|---|---|---|
| 导出的歌词播放器不认 | `validator.py`、`exporter.py` | `wordtime.py`（逐字轴） |
| 逐字文字和歌词对不上 | `web/document.js` 的 `retargetWords` | `wordtime.py` 的 `tokenize` |
| 插行/删行报时间错误 | `edits.py` + `document.check` | `web/document.js` 同名函数 |
| 波形点了没反应 / 按钮全死 | 页面脚本加载顺序（`index.html`） | `web/common.js` 的 `bind()`、`tests/web_dom_check.js` |
| **某个控件点了完全没反应** | **`Ctrl+Shift+T` 看追踪面板**（该手势后有没有任何结果） | 若日志里没有这个手势 → 按钮**没接线**：查 `bind()` 与页面书写顺序（`test_web_pages.py` 会报）；若有手势、无结果 → 处理函数**静默返回** |
| 脚本绑定了写在其后的元素 | `tests/test_web_pages.py::test_no_script_binds_an_element_declared_later_in_the_page` | `<script src>` **阻塞解析**，只能绑定它前面的元素 |
| **弹窗没居中/贴页面底边/没有遮罩/关不掉** | `style.css` 里给 `<dialog>` 设了 `display` 的那条规则 | 作者样式**压过 UA 的 `dialog:not([open]){display:none}`（与优先级无关）**；必须有 `dialog:not([open]){display:none !important}`。**没有遮罩 + 垂直贴底 = 它不是模态，是没隐藏** |
| 布局在大屏上塌了 | `style.css` 的 `@media(min-width:1250px)` | `web/splitter.js`（它会插入第 3 个子元素） |
| 写入 0 字节 / 没权限 | `server.py` 的 `_save` / `_export_folder` | `folders.py` |
| **选文件夹报 `access violation reading 0x…` / 对话框看不到** | `folders.py` 的 `_win32()`：每个 Win32 调用都要声明 `argtypes` + `restype`（指针类返回值不声明就是 `c_int`，64 位下**截断**） | 判据：**异常里的地址只有 32 位**（高 32 位全 0 或全是 F）；守它的是 `tests/test_folders.py`（假 DLL 驱动整条管道 + 真 shell32 的无窗口往返）。**对话框本身不测**（不许开窗口），所以别再靠"手点一下看看" |
| **工程没自动保存 / 保存路径没被问到 / 退出时没问"保留还是删除"** | `web/projects.js`（`Project.trackAll` 挂在 `repaintQueue` 上、`sync` 挂在 `persist` 上） | 路由 `api/projects.py`；L2 边界 `projects.py`。**先看 `Ctrl+Shift+T` 有没有 `工程已建`/`退出确认`** |
| **工程目录被误删 / 删到了保存路径里的别的东西** | `projects.py` 的 `_inside` + `delete`（只在保存路径**之内**删） | `discard_session` 只删本次会话建的；测试 `tests/test_projects.py` 全是这条边界 |
| 跑测试把用户的保存路径改了 | `store.config_dir()` 认 `SALTLRC_CONFIG_DIR`（测试必须设它） | `tests/test_web_dom.py` 的 `setUpClass` |
| 对齐 0 行可用 | `alignment.py` 的 `source_track`、`acoustic.py` 的 `refine_words` | `docs/AUDIO_ALIGNMENT.md` |
| **智能贴唱贴歪了 / 整首乱序 / 按了没反应** | `fit.py`（`analyse()` 找人声起点、`fit_lines()` 单调路径） | 页面侧唯一权威是 **`timelineGroups(item)`**（预览时「按时间找行」都从它取）；守它的分别是 `tests/test_fit.py`（算法真值）、`tests/web_align_check.js`（页面接线）、`tests/test_align_routes.py`（真 HTTP）。**代价函数与识别口径写在 `fit.py` 模块头，改之前先读它** |
| 打包后一运行就 NameError | `app_entry.py`、`jobs.py`（`--worker`） | `packaging/build.ps1`、`smoke-exe.ps1` |
| 新 exe 行为和源码不一致 | 交付比对：`packaging/` 的哈希核对 | 残留进程会锁住 exe，复制会**静默失败** |

## 测试怎么跑

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests   # 459 项，全绿（允许 skip）
```

**前端检查已经全部包在 Python 测试里，不必单独手跑**：
`test_web_dom` 会起一个临时服务并以 jsdom 加载**真实页面**，
内部再驱动 `web_dom_check.js`（页面接线）、`web_library_word_check.js`（曲库逐字编辑器，
27 项）、`web_insert_e2e_check.js`（插行端到端）、`web_project_check.js`（本地工程库端到端：
设路径 → 导入建工程 → 编辑落盘 → 退出问两次 → 真删真留）、`web_late_match_check.js`、
`web_playback_seek_check.js`、`web_track_hint_check.js`；
`test_web_pages` 里则挂着纯静态的**脚本/元素顺序**检查与其余前端行为检查。
`web_block_drag_check.js`、`web_document_check.js` 走 `web_load_harness.js`。

> **后台服务要先关掉再跑测试**：`test_web_dom` 自己会起
> `ThreadingHTTPServer(('127.0.0.1', 0))`，与常驻服务抢资源时会出现
> 「button is wired FAIL」的**假失败**。

**可选依赖不进测试环境也没关系**：程序本身 `dependencies = []`，唯一 import 三方库的
地方是 `transcriber.py`（可选 ASR）。所以 `tests/test_adapters.py` 对 `numpy` 与
`faster_whisper` 都打桩（打的是三方库，不是本项目的层），另有一条断言钉住
"缺依赖时必须给出**中文可读原因**"——`ModuleNotFoundError` 不是 `ValueError`，
会落到 500「服务器内部错误」。**这套测试在装没装可选依赖的机器上都能得出结论**，
所以发布闸门（`packaging/release.ps1` 第 2/6 步）只认全绿，没有"已知失败"这一档。
