---
name: biomed
description: 紧凑、可信、以真实数据为先的局域网生信工作台
colors:
  background: "oklch(1 0 0)"
  foreground: "oklch(0.148 0.004 228.8)"
  primary-teal: "oklch(0.511 0.096 186.391)"
  primary-foreground: "oklch(0.984 0.014 180.72)"
  muted-mist: "oklch(0.963 0.002 197.1)"
  muted-foreground: "oklch(0.56 0.021 213.5)"
  border: "oklch(0.925 0.005 214.3)"
  destructive: "oklch(0.577 0.245 27.325)"
  chart-cyan-1: "oklch(0.865 0.127 207.078)"
  chart-cyan-5: "oklch(0.45 0.085 224.283)"
typography:
  title:
    fontFamily: "Public Sans Variable, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.4
  body:
    fontFamily: "Public Sans Variable, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Public Sans Variable, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.5
rounded:
  base: "0.45rem"
  sm: "calc(0.45rem * 0.6)"
  md: "calc(0.45rem * 0.8)"
  lg: "0.45rem"
spacing:
  control-gap: "0.5rem"
  section-gap: "1.25rem"
  content-mobile: "1rem"
  content-desktop: "1.25rem"
components:
  primary-action:
    backgroundColor: "{colors.primary-teal}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.md}"
  data-container:
    backgroundColor: "{colors.background}"
    textColor: "{colors.foreground}"
    rounded: "{rounded.lg}"
---

# Design System: biomed

## Overview

**Creative North Star: “可信的实验台”**

这是一个供项目所有者在局域网内高频使用的生信工作台，采用官方 shadcn Mira preset：`radix-mira`、mist neutrals、teal theme、cyan charts、Lucide、Public Sans、小圆角和克制的菜单强调。界面属于 **Operate** 模式，优先保证扫描速度、数据密度、真实状态和键盘操作，不承担营销展示职责。

信息架构分为知识库、证据库、生信工具与管理。首页以标准实体查询为主入口；知识库首先是基因、其次是疾病，药物保留独立入口。证据库包含临床证据和文本资料，工作流属于管理。

**关键特征：**

- 紧凑 sidebar + data-first 主内容。
- 单一 teal 行动色，mist 中性色承载层级，cyan 仅用于真实图表序列。
- 状态文字、可点击性和后端能力一致；缺失能力明确写“规划中”或“不可用”。
- 图标统一使用 Lucide，同类图标保持相同尺寸与描边语气。

## Colors

白色与 mist neutral 构成安静、高对比的工作底色；teal 只标记主要行动、选中态和焦点，避免满屏着色。

- **Primary Teal** (`oklch(0.511 0.096 186.391)`): 主按钮、当前导航、关键可交互状态；同一视区只保留一个视觉主行动。
- **Mist Surface** (`oklch(0.963 0.002 197.1)`): 次级背景、悬停与低强调容器。
- **Ink Foreground** (`oklch(0.148 0.004 228.8)`): 标题、正文和关键数据。
- **Mist Muted Foreground** (`oklch(0.56 0.021 213.5)`): 说明、元数据和非关键状态；不得用于需要强对比的核心结果。
- **Cyan Chart Scale** (`chart-cyan-1` 至 `chart-cyan-5`): 仅用于有真实数值含义的图表系列，不用于装饰卡片。
- **Destructive** (`oklch(0.577 0.245 27.325)`): 只用于真实错误、失败或破坏性操作，不用于“规划中”。

深色模式沿用同一语义 token，不在组件内硬编码明暗颜色。正文与占位文字对背景至少达到 4.5:1，大字号文本至少达到 3:1。

## Typography

全局使用 **Public Sans Variable**，标题与正文同族，通过字号、字重和间距建立层级。等宽字体只用于序列、变异表达式、代码和固定宽度数据，不作为“科研感”装饰。

- **页面标题:** `1.25rem / 600`，紧凑但明显高于区块标题。
- **区块标题:** `0.875rem / 500–600`，直接命名内容，不添加 eyebrow 或无信息量编号。
- **正文与控件:** `0.875rem / 400–500`，说明性长文控制在约 65–75ch。
- **标签与元数据:** `0.75rem` 或现有 `0.6875rem`，仅用于次级信息；重要状态不可依赖极小字号。
- 数据表数字使用 tabular numerals；基因、转录本、蛋白质和变异标识保持原始大小写且允许选择复制。

## Layout

- 桌面端使用可折叠的紧凑 sidebar，主内容区按数据和任务关系组织；禁止用一组同尺寸、同权重的 admin 卡片作为页面骨架。
- 门户首屏查询真实实体，下方并列标准库和资料工作流。统计均读取后端，不展示虚构进度。
- 生信区保留实际可用的变异注释、任务历史、氨基酸映射。尚未实现的序列工具和 BLAST 不占主导航。
- 内容区以 `1rem` 移动端内边距、`1.25rem` 桌面内边距和 `1.25rem` 区块间距为基准；相关控件靠近，章节之间留出更明显间隔。
- 桌面可用非对称列突出 Gene（主列约为辅列的两倍）；移动端改为单列，顺序与桌面阅读顺序一致，不横向溢出。
- sticky 顶栏保持紧凑；侧栏折叠后仍保留图标、tooltip、当前项和可访问名称。

## Elevation & Depth

默认以边框、分隔线和轻微色调差建立层级，不用装饰性玻璃、泛光或厚重阴影。浮层可使用 shadcn/Radix 的克制阴影表达真实叠层；静态内容容器保持平面。焦点环是交互反馈，不作为装饰光晕。

## Shapes

基础圆角为 `0.45rem`，控件和容器使用 Mira 的 small-radius 比例。边框保持细、低对比且语义一致；不要引入药丸化卡片、巨型圆角容器或粗彩色侧边线。图表、数据表和代码区的外形服从内容密度，不额外套嵌多层卡片。

## Components

### Navigation

- 分组为工作台、知识库、证据库、生信工具、管理；文本资料下嵌套文献管理与指南管理。转录本保留在基因详情中，教学验证与正式证据入口明确区分。
- active 使用 subtle menu accent；hover、focus、active 必须可区分，不能只靠颜色。
- 不可用项不提供链接，保留禁用语义，并通过可见文字或 tooltip 说明“规划中”。

### Gene search

- 是门户唯一高强调入口，搜索框与主按钮必须在首屏快速可见。
- 支持真实标识类型的示例和清晰 label；提交后进入真实 Gene 搜索，不展示模拟结果。
- 加载、无结果、失败和成功状态分别表达；失败信息说明问题与恢复方式。

### Database and tool entries

- 未接入的数据库不提供虚假检索入口。
- 序列工具箱和 BLAST 在本轮为不可点击项。变异注释仅当真实引擎状态为 ready 时显示“可用”并提供入口；状态未知、读取失败或没有 ready 引擎时不得推断可用。
- 状态 badge 只总结状态，旁边的文字负责给出原因或下一步；“规划中”使用中性样式，错误才使用 destructive。
- loading 使用骨架或明确的读取文案，避免布局跳动；empty 表达“没有数据/任务”，不可伪装成错误。

### Buttons, inputs, containers and data

- 主按钮只用于当前主要动作；次要动作使用 outline/ghost/link 层级。disabled 必须在视觉与原生语义上同时成立。
- 输入框始终有可访问名称、可见焦点和明确错误关系；placeholder 只作示例，不替代 label。
- 容器优先承载一组相关数据或操作；避免 nested cards。列表用分隔线，详情用 definition list 或表格。
- 表格保留表头、列语义和键盘/读屏顺序；窄屏优先重排关键字段，确需横向滚动时提供清晰滚动容器。

## Do's and Don'ts

### Do

- **Do** 让界面状态直接来源于后端数据、引擎健康或浏览器本地能力。
- **Do** 为所有交互提供 hover、focus-visible、disabled、loading、empty、error 和成功反馈。
- **Do** 保持 44px 左右的移动端触控目标；桌面高密度控件仍需清晰焦点和足够点击范围。
- **Do** 保持 DOM 顺序与视觉顺序一致，使用语义 heading、landmark、label、表头和 `aria-live` 状态通知。
- **Do** 扩展数据库或工具时复用现有行式条目、状态模型和语义 tokens，并验证桌面与移动端。

### Don't

- **Don't** 把规划能力做成可点击入口、伪造统计、模拟结果或用含糊的“在线”代替真实状态。
- **Don't** 新增第二个与 Gene 搜索争夺首屏注意力的主入口。
- **Don't** 将标准实体、真实证据与教学样例混入同一目录，或使用无差别卡片网格。
- **Don't** 用颜色作为状态的唯一线索，或用 tooltip 承载完成任务所必需的信息。
- **Don't** 为新入口硬编码颜色、圆角、字体或状态文案；必须复用语义 token 和统一状态词汇。

新增数据库或工具的最低准入条件：有明确所属分组、真实数据/执行链路、可判定的可用状态、加载/空/错状态、权限与数据来源说明，以及响应式和键盘验收。未满足这些条件时，只能作为明确不可用的规划项，不能提供导航链接或主要行动按钮。
