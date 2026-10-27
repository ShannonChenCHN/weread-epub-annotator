---
name: weread-epub-preview
display_name: 微信读书 EPUB 本地预览 + 合规体检
description_zh: 把 EPUB 一键渲染成「微信读书模拟器」预览页（手机壳固定 Apple 官方设计稿的 iPhone 画板尺寸 402×874 并按物理尺寸缩放 + 单一白底阅读主题 + 模拟层全开按真机默认行为），并按 24 条真机实测规则生成静态体检报告（死/灰/提示，定位到文件行号）。导真机前先在本地过一遍，省掉反复上传。
description_en: Render an EPUB into a local WeRead-style preview (phone frame fixed at Apple's official design-template iPhone artboard 402×874, physically scaled to match a real device, single white reading theme, emulation layer always on with real-device defaults) plus a static compliance report based on 24 real-device-tested rules. Trigger on: 预览 epub、本地预览、不想反复上传微信读书、检查样式会不会失效、epub 体检。
description: >
  当用户说「不想每次都上传微信读书看效果」「本地预览 epub」「改完样式能不能先在本地看」「检查这份
  epub 在微信读书里会不会出问题」「体检/检查 epub」时使用。
  一条命令产出 index.html（单文件预览页，file:// 直接打开或放进 WorkBuddy 预览面板）+
  weread-report.md（体检报告）。预览层按真机默认行为（模拟层全开，不可切换）模拟：丢弃复杂选择器与
  @media、丢弃内联 style、忽略 text-indent/line-height、px 字号压回正文、图片按版心等比缩放、hr 右边距置 0；
  与 weread-epub-style（排版规范）配套使用。
  触发词：本地预览、epub 预览、微信读书预览、模拟器、体检、合规检查、不导真机看效果。
agent_created: true
category: 工具
version: 1.0.0
---

# 微信读书 EPUB 本地预览 + 合规体检

## 一句话

```
python3 ~/.workbuddy/skills/weread-epub-preview/scripts/weread_preview.py <book.epub> [--open] [--force]
```

产出（默认 `<epub 同级>/<书名>.preview/`）：

| 文件 | 用途 |
| --- | --- |
| `index.html` | 单文件预览页，file:// 直接打开；也可交给 present_files 在 WorkBuddy 预览面板里打开 |
| `weread-report.md` | 体检报告（人读，按死/灰/提示分组，定位到文件:行号） |
| `weread-report.json` | 同上，机器读 |

再次运行时自动复用解包缓存（比对 EPUB 的 mtime+size），改完样式重跑一次即可，秒级。
`--force` 强制重新解包；也可以直接传**已解包目录**作为输入。

## 预览页里有什么

- **手机壳 + 版心**：固定为 **Apple 官方设计稿（Apple iOS 27 UI Kit）的 iPhone 默认画板尺寸 402 × 874**（CSS px，DPR 3，不可切换）。
  外壳宽 432 = 屏幕 402 + 两侧各 15px 黑边（≈真机 2.45mm 边框；边框过薄会让外壳比例偏长）；高度**固定 874**，
  不随窗口高度变化（窗口太矮时舞台内部滚动），外壳高宽比 2.093 ≈ 真机机身 2.092。
  版心左右各 20px → **正文列宽 362px**，与真机微信读书基本一致；用来检查表格和扫描图会不会溢出。
- **物理尺寸矫正 `zoom:.8`**：手机上 1 CSS px ≈ 0.166mm，显示器上 1 CSS px ≈ 0.20~0.23mm，
  按 1:1 画会比真机大 20%~40%（并排对比就会觉得「长不少」）。缩到 0.8 后外壳物理大小贴近真机、字号也是同尺度观感；
  想看最大最清楚的字，把 `.phone` 的 `zoom` 改成 `1`（布局一字不变）。窄屏模式下强制 `zoom:1`，正文照旧铺满。
- **阅读主题**：固定 **白底**（theme-white，不可切换）。
- **模拟层（默认全开，不可切换）**：按真机默认行为生效——丢弃复杂选择器 / @media、丢弃内联 `style`、忽略 `text-indent`/`line-height`、px 字号压回正文、图片按版心等比缩放、`hr` 右侧边距置 0。这些就是「真机上样式会被丢掉」的部分；字号(17px)/行距(1.8)/首行缩进(2字)用真机默认全局设置，直接套用到正文。
- 深链：`index.html#c=16` 直接打开第 17 个 spine 文件（翻章靠目录，不再有键盘快捷键）。
- **窄屏自适应（≤900px，手机 / WorkBuddy 预览面板）**：目录变为左侧抽屉
  （背景遮罩点按关闭），说明折叠进顶栏按钮（目录·说明），
  手机壳撤掉、正文铺满全宽——保证书本内容不被周边面板遮挡。
  顶栏 `sticky` 吸附顶部、滚动全程不动（窄屏媒体查询里把 `html,body` 放开为 `height:auto`，
  否则 body 被全局 `height:100%` 锁成一屏高，滚动超过一屏后顶栏会被「拽走」）。
- **选中即批注（内置，默认开）**：预览页默认带「✏️ 标注 / 📋 批注(n)」两个顶栏按钮。
  开启「✏️ 标注」后，在正文用鼠标选中文字或图片即可弹批注框（严重级 P1/P2/P3 + 文字/图片/版式 + 备注），
  按「源 xhtml 路径 + 文字片段 + 最近标题」定位（源码无 id 也能反查），存 localStorage，换章自动重绘高亮；
  「📋 批注」面板可跳转定位、导出 JSON、导出「给 agent 的 MD 任务清单」、清空。
  由 `--annotate`（默认开）/ `--no-annotate`（纯预览）控制；历史预览仍可用配套 `inject_annotate.py` 后处理注入。
  批注层只改预览页 HTML，**不碰 EPUB 源**；重跑预览会清掉注入层，但 localStorage 里的批注数据仍在浏览器侧。
- 静态体检报告仍会生成 `weread-report.md` / `weread-report.json`（见上方产出表），只是预览页内不再内置报告抽屉。

## 体检规则来源

`references/weread-style-support.json`（24 条，源自用户自建样张书真机实测仓库
`weread-epub-style-support`）。检查项包括：R01 text-indent、R02 line-height、R03 px 字号、
R04 单一字体名、R05 复杂选择器/@media、R06 内联 style、R07 图片 CSS 定宽、R08 figure 内 img 居中、
R09 float、R10 硬分页、R10b 同文件多 h1/h2、R11 弹注/锚点、R12 flex/transform/text-shadow、
R13 small-caps、R14 CJK 扩展 B 区生僻字、R15 @font-face、R16 hr+% 边距、R17 `<p><img>`、
R18 `<img width height>`；结构检查 S01 XML 不合法（真机会白屏）、S02 nav 混进 spine、
S03 manifest 缺文件、S04 资源未注册、S05 元数据缺失、S06/S07/S08 体积与扫描图分辨率。

## 实现要点（改代码前先读）

- 章节内容挂进 **Shadow DOM**：书籍自带 CSS 原样注入 shadow root，不会污染预览 UI 外壳；
  注入前把选择器里的 `html`/`body`/`:root` 重写成 `.wr-body`。
- 章节**按需挂载**：整本书的 HTML 放在 `<script type="application/json" id="wr-data">` 里
  （`<` 已转义成 `\u003c`），切章时才 innerHTML，扫描书不会一次加载全部图片。
- 模拟层是真的在 **CSSOM** 上动手（`deleteRule` / `removeProperty` / `setProperty`），
  不是假装；所以开关切换走 `build()` 重建。
- URL 重写：章节里的相对路径 `../images/x.jpg` 会按章节所在目录规约成「相对解包根目录」的路径，
  因此 `index.html` 必须放在解包根目录里才能显示图片。
- 章节标题优先取 nav/ncx，缺了退回 `<h1-4>`/`<title>`。

## 与其他 skill 的配合

- 生成/改造 EPUB 的 CSS 用 **`weread-epub-style`**（规则与配方）；
- 本 skill 负责「改完先本地看 + 体检」，**`ebook-conversion-proofread`** 负责「转完逐字校对」；
- 真机快通道（最后验收仍要做）：微信读书网页版 weread.qq.com 登录同一账号 →
  「导入书籍 / 传书到手机」拖入 EPUB → 约 10 秒同步到手机 App 书架。
  ⚠️ 网页版与 App 是否同一渲染引擎未实测，只当提速通道，字体效果仍以 App 为准。

## 边界（诚实说明）

- 本预览是**模拟**，不是微信读书的自研排版引擎（DTCoreText）。颜色/粗细/字号/背景/边框/
  表格/列表/图片缩放等可信；**字体栈**（汉仪楷体S）本机多半没有，楷体引文会回退——字体效果必须真机确认。
- 分页、弹注、锚点跳转这三类行为真机差异大，预览只做体检提示，不模拟交互。
