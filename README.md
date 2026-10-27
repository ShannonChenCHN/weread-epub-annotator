# weread-epub-annotator

> 微信读书风格 EPUB 本地预览 + 合规体检 + **选中即批注**

把任意 EPUB 一键渲染成「微信读书模拟器」预览页，在**本地**就能看到真机排版效果、跑合规体检，并直接在正文里**选中文字/图片即可批注**，导出 Markdown / JSON 任务清单交给校对或 Agent 处理——省掉反复上传微信读书的来回。

## 功能特性

- **微信读书风预览**：单文件 `index.html`（可 `file://` 直开，或放进 WorkBuddy 预览面板）。
  - 手机壳固定 Apple 官方设计稿 iPhone 画板尺寸 402×874，按物理尺寸缩放，贴近真机观感；
  - 单一白底阅读主题；模拟层按真机默认行为全开（丢复杂选择器 / 去内联 style / 忽略 text-indent+line-height / px 字号压回正文 / 图片按版心等比缩放）。
- **合规体检**：按 24 条真机实测规则生成静态报告（`weread-report.md` + `weread-report.json`），分死 / 灰 / 提示三档，定位到文件行号。
- **选中即批注（内置）**：预览页顶栏「✏️ 标注」开启后，在正文选中文字/图片 → 弹框填严重级（P1/P2/P3）+ 类型（文字/图片/版式）+ 备注 → 保存。
  - 批注按「源 xhtml 路径 + 文字片段 + 最近标题」定位，换章自动重绘常驻标记（左侧按严重级上色条）；
  - 「📋 批注」面板可点条目滚动定位并**闪烁高亮**对应内容，支持导出 JSON / 导出给 Agent 的 MD 任务清单 / 清空；
  - 数据存浏览器 `localStorage`，不依赖任何外部服务。

## 安装（作为 WorkBuddy 技能）

```bash
# 放到用户级技能目录（任选其一）
git clone https://github.com/ShannonChenCHN/weread-epub-annotator.git \
  ~/.workbuddy/skills/weread-epub-preview
```

> 技能目录名需为 `weread-epub-preview`（与 `SKILL.md` 的 `name` 一致）；克隆后可改目录名或建软链。

也可在 WorkBuddy 的技能市场 / 专家中心直接安装本仓库发布的技能版本。

## 命令行用法

```bash
python3 weread_preview.py book.epub                 # 生成预览（默认内置批注层）
python3 weread_preview.py book.epub --open --force  # 强制重解包并生成后用浏览器打开
python3 weread_preview.py book.epub --no-annotate   # 纯预览，不含批注层
python3 weread_preview.py /path/to/unpacked_dir     # 直接对解包目录生成
```

参数：

| 参数 | 说明 |
| --- | --- |
| `--annotate` / `--no-annotate` | 是否内置「选中即批注」层（默认开） |
| `--force` | 强制重新解包 EPUB |
| `--open` | 生成后自动用浏览器打开 |
| `--ui light\|dark` | 预览外壳配色（默认 light） |
| `--img-max N` | 内嵌图片最长边上限 px（默认 720，0=不缩放） |
| `--no-embed-img` | 不内嵌图片，保留相对路径（file:// 直开时用） |

## 批注工作流

1. 打开预览页 → 点「✏️ 标注」（默认已开启）→ 在正文选中文字或图片；
2. 弹框选严重级 / 类型、写备注 → 保存；左侧出现对应色条（常驻总览）；
3. 点「📋 批注」打开面板 → 点条目：页面滚动到对应段并**闪烁高亮**；
4. 导出「JSON」（结构化）或「导出MD」（给 Agent 的修复任务清单，含源路径 + 锚点 + 严重级 + 备注）。

## 目录结构

```
weread-epub-preview/
├── SKILL.md                      # 技能元数据与使用说明（WorkBuddy 读取）
├── scripts/
│   ├── weread_preview.py         # 预览生成器 + 合规体检（内置 --annotate 开关）
│   └── inject_annotate.py        # 「选中即批注」层（CSS/HTML/JS 单一来源，被生成器 import）
├── references/
│   └── weread-style-support.json # 微信读书样式支持度映射（合规体检用）
├── LICENSE
└── README.md
```

> **批注层单一来源**：`inject_annotate.py` 导出的 `CSS / HTML / JS` 是批注功能的唯一实现，生成器 `weread_preview.py` 在渲染时按 `--annotate` 开关原样注入，避免两份实现漂移。

## 开发 / 贡献

- 改动批注层只需编辑 `scripts/inject_annotate.py`；重新生成预览即可生效。
- 预览页是超大单文件（图片内嵌为 data URI），批注 JS 嵌在 `<!-- wr-anno:js -->` 标记之间，方便定位。
- 提交信息建议说明「改了哪一类行为」（预览 / 体检 / 批注交互），便于版本回溯。

## License

[MIT](./LICENSE) © 2026 Shannon Chen
