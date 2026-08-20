# scys-mcp 取数模式

仅在对应能力真实可调用时使用本模式。工具名称可能带服务前缀，以功能描述和参数合同为准。

## 必需能力

需要三类只读操作：

1. 航海活动搜索或当前用户航海列表；
2. 航海手册完整目录；
3. 按目录条目读取手册正文。

已知工具通常为：

- `activitySearch` 或 `activityList` / `listMyActivities`；
- `activityManualToc`；
- `activityManualDetail`。

目录和正文工具不可用时，不要拼接私有 API，切换到网页回退模式。

## 解析活动

用用户给出的手册名称或页面标题调用活动搜索 / 列表，核对：

- 活动名称；
- 活动期次；
- 是否为用户目标；
- 当前用户是否有权限。

`activityId` 必须来自搜索或列表结果，不要只从 URL 猜测。存在多个候选时让用户选择。

## 读取目录

使用同一个 `activityId` 调用手册目录。保存本次响应中的：

- `activityId`、`courseId`、`courseTitle`、`manualType`；
- 每个条目的 `itemId`、`title`、`level`、`parentId`、`hasContent`；
- 原始目录顺序。

只向正文工具传入同一次目录查询返回、且 `hasContent=true` 的 `itemId`。不要跨活动复用条目 ID。

## 读取正文

对每个目标条目调用正文工具：

```text
activityId = 本次目录的 activityId
itemId = 本次目录返回的 itemId
format = markdown
maxChars = 工具允许的较大安全值
offset = 0
```

如果 `truncated=true`，把 `nextOffset` 原样传入下一次调用，直到 `truncated=false`。按 offset 拼接正文；不得只读第一段就宣称完整。

遇到 `MCP_RATE_LIMITED` 时按返回的 `retryAfterSeconds` 等待，不猜固定限额。权限错误、条目不存在或连续读取失败时停止该活动，报告具体小节，不切换到搜索结果或其他航海替代。

## 识别 Plain Text

优先使用响应中的显式语言元数据。如果新手册的 Markdown 响应只保留围栏：

- 无语言标记的代码围栏视为页面的 Plain Text 区块；
- 带 `markdown`、`bash`、`json`、`python` 等语言标记的围栏不收集；
- 不根据内容“看起来像提示词”扩大范围。

逐条保留围栏内部的全部字符、空行、标点和末尾换行。记录内部证据：活动、篇、关卡、小节、条目 ID、区块顺序、最近标题和最近说明。这些证据用于组织和验收，不写进最终笔记。

## 构建目录层级

- `level=1`：篇 / 顶层分组；
- `level=2`：关卡 / 章节笔记；
- `level=3`：合并进父级关卡笔记的小节；
- 平铺目录：按目录条目顺序每篇一份笔记。

如果层级字段不同，以 `parentId` 和实际标题关系重建树，不依赖固定关卡数量。

## 完整性报告

在预览中报告：

- 目录条目总数；
- 有正文的条目数；
- 成功完整读取数；
- 读取失败或截断未完成的条目；
- 每个关卡和全手册的 Plain Text 数量。

只要有目标正文未完整读取，就把交付标为不完整，不宣称全量收集完成。
