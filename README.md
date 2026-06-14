# 炼化钳 (astrbot_plugin_group_digest)

实时采集 QQ 群聊文本消息，定时调用 LLM 生成结构化摘要，以图片或 Markdown 文件形式输出到群聊。

## 基本功能

- **消息采集** — 监听群聊/私聊文本消息，流式追加到按时间命名的汇总文件，确保日志不会丢弃任何记录
- **定时摘要** — 可配置多个触发时间点，自动调用 LLM 生成摘要
- **组合摘要** — 支持携带 N 个历史汇总文件作为上下文，生成更连贯的摘要

## 工具属性
- **输出方式** — pillowmd 本地渲染为图片，或直接发送 .md 文件
- **自动转发** — 摘要/原始日志可静默转发到指定群聊或私聊（不附加来源标注）
- **双向命令** — send/receive 系列命令支持跨会话转发摘要/原始日志
- **匿名模式** — 可关闭昵称记录，仅采集消息内容
- **静默操作** — 可关闭通知提示，他们不会察觉自己被炼化

## 格式预览

> **原始md节选**
```
[夏安] 吃不了船长
[狂奔的蜗牛] 速拿任务道具
[pve0级萌新] 上层还能爬？
[夏安] 能
[pve0级萌新] 6
[狂奔的蜗牛] 主要是卡任务了，摸摸c1和机油
[Nskrd] 能
[Nskrd] 需要清停机坪和炸药门门口
[雇佣兵] 来不来
[狂奔的蜗牛] 也不用清炸药门
[狂奔的蜗牛] 我没清
```

> **摘要markdown形式节选** 

![[屏幕截图 2026-06-14 171600.png|589]]

> **摘要图片形式（与上文无关）** 

![[渲染图片示例.jpg]]

### 自动转发行为

当 `转发目标群号列表` 或 `转发目标QQ号列表` 非空时，所有摘要/原始日志在正常发送后自动静默转发到配置目标：

- 转发**永远静默**，不在当前会话产生任何通知
- 适用于定时摘要和 `generate` 命令触发两种场景

## 命令

### mode 参数说明

| mode | 当前会话通知 | 目标消息来源标注 |
|------|-------------|-----------------|
| `public`（默认） | 🔄/✅ 进度通知 | `来源: {群名称}({群号})` 或 `来源: 私聊 {昵称}({QQ号})` |
| `private` | 无（静默） | 无 |

> `suppress_notifications: true` 时，public 模式也强制静默。

### coupling_count 参数说明

大部分命令当中默认为 0 或者 1，原始文件组合的数目为 `coupling_count` +1

> 根据时间倒序（从近到远）选取原始文件进行组合然后再操作，为 0 则只选取实时的原始记录文件
### send（当前会话 → 目标）

| 命令         | 参数                                    | 说明                                                                        |
| ---------- | ------------------------------------- | ------------------------------------------------------------------------- |
| `/sendmd`  | `<target_id> [mode] [coupling_count]` | 生成 AI 摘要发送到目标。仅输入sendmd则使用默认命令`sendmd 0 public 1`  当前会话的近两次日志将被总结并发送到当前群里 |
| `/sendraw` | `<target_id> [mode] [coupling_count]` | 发送原始汇总日志（.md 文件），不经过 AI。参数同上                                              |

### receive（源会话 → 当前会话）

| 命令            | 参数                                    | 说明                                                                       |
| ------------- | ------------------------------------- | ------------------------------------------------------------------------ |
| `/receivemd`  | `<source_id> [mode] [coupling_count]` | 从源会话生成 AI 摘要，发送到当前会话。`mode` 与 `coupling_count` 留空则分别使用默认值 `public` 和 `1` |
| `/receiveraw` | `<source_id> [mode] [coupling_count]` | 从源会话获取原始日志，发送到当前会话                                                       |

### 批量 & 管理

| 命令               | 参数          | 说明                                            |
| ---------------- | ----------- | --------------------------------------------- |
| `/generate`      | `<md\|raw>` | 对所有启用群聊批量生成摘要/日志，静默转发到配置的转发目标。如果没有配置转发目标则不会执行 |
| `/checksize`     | `<target_id> [coupling_count]` | 查看目标会话汇总文件统计（文件数、总大小、总行数），默认查看当前会话 |
| `/forcecut`      | `<target_id>` | 强制结束当前活跃汇总文件，立即创建新的空白文件。默认操作当前会话 |
| `/digest_status` | 无           | 查看当前会话采集状态（活跃文件、消息条数、历史文件数）                   |
| `/digest_clear`  | 无           | 清空当前会话所有汇总文件                                  |
| `/digest_config` | 无           | 查看当前插件配置                                      |

### 使用示例

```
# send
/sendmd 0                                 # 生成摘要发到当前会话（public，有通知）
/sendmd 0 private                         # 生成摘要发到当前会话（完全静默）
/sendmd 123456789 public 2                # 生成摘要发到群 123456789，耦合 3 个历史文件

# sendraw
/sendraw 0                                # 发送原始日志到当前会话（public）
/sendraw 123456789 private 2              # 耦合 3 个历史文件，静默发到群 123456789

# receive
/receivemd 123456789                      # 从群 123456789 取摘要，发到当前会话
/receiveraw 987654321 private             # 从 QQ 987654321 取原始日志，静默

# 批量 & 管理
/generate md                              # 批量生成所有群的 AI 摘要并转发
/generate raw                             # 批量发送所有群的原始日志并转发
/checksize 0                              # 查看当前会话汇总统计
/checksize 123456789 2                    # 查看群 123456789 的统计，耦合 3 个历史
/forcecut                                 # 强制分割当前会话的活跃文件
/forcecut 123456789                       # 强制分割群 123456789 的活跃文件
/digest_status                            # 查看当前采集状态
```

## 数据存储

汇总文件存储在 AstrBot 数据目录下：

```
data/plugin_data/astrbot_plugin_group_digest/
├── group_123456/
│   ├── 25-06-14-08-00.md   ← 已摘要的历史文件（文件名 = 创建时间）
│   ├── 25-06-14-14-30.md   ← 当前活跃文件（新消息流式追加至此）
│   └── ...
└── private_789012/
    └── ...
```

定时摘要使用 `coupling_count` 配置项决定耦合深度；手动命令默认 coupling=1（当前文件 + 前 1 个历史）。

## 依赖

- **pillowmd**：图片渲染引擎

对于无html渲染markdown借鉴了以下插件的实现
> https://github.com/luosheng520qaq/astrbot_plugin_nobrowser_markdown_to_pic 


# 开发文档

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_groups` | list | `[]` | 监控的群号列表 |
| `enable_private` | bool | `true` | 是否采集私聊 |
| `forward_groups` | list | `[]` | 自动转发目标群号 |
| `forward_private` | list | `[]` | 自动转发目标QQ号 |
| `modality.text` | bool | `true` | 采集文本消息 |
| `modality.image` | bool | `false` | 采集图片占位 `[图片]` |
| `modality.voice` | bool | `false` | 采集语音占位 `[语音]` |
| `summary_schedule` | string | `08:00,20:00` | 定时触发时间（HH:MM逗号分隔，留空关闭） |
| `scheduled_summary_groups` | string | `""` | 定时摘要黑名单（逗号分隔群号） |
| `coupling_count` | int | `0` | 定时摘要耦合历史文件数 |
| `self_recursion` | bool | `false` | 是否采集机器人自身消息 |
| `name_record` | bool | `true` | 是否记录 `[群昵称]` |
| `cache_clean_days` | int | `7` | 文件保留天数 |
| `cache_clean_interval_hours` | int | `12` | 清理检查间隔（小时） |
| `summary_prompt` | text | (见默认) | AI 摘要提示词模板 |
| `summary_provider_id` | string | `deepseek/deepseek-v4-flash` | LLM 模型 ID |
| `output_mode` | string | `image` | 输出方式：`image` 或 `file` |
| `suppress_notifications` | bool | `false` | 全局静默，禁止所有通知和来源标注 |
| `style_path` | string | `""` | pillowmd 渲染风格目录，留空用内置 modern-dark |

---

## 新日志文件创建时机

文件命名 `YY-MM-DD-HH-MM.md`，由 KV 键 `active_file_{session_key}` 追踪。

| 节点 | 触发条件 | 触发方 |
|------|---------|--------|
| **启动预创建** | 已配置群的目录下无 .md 文件 | `initialize()` → `_pre_create_active_files()` |
| **首条消息** | KV 中无 active_file 记录 或 文件已不存在 | `_handle_message()` → `_append_message()` |
| **摘要完成** | 定时任务、`/sendmd`(target=0)、`/generate`、`/receivemd` 成功后 | `do_summary()` / `cmd_sendmd()` → `_create_new_active_file()` |
| **清空后重建** | `/digest_clear` 删除所有文件，下次消息触发首条消息逻辑 | `cmd_digest_clear()` → 清空 KV → 节点2 |

**旧文件不删除**：摘要完成后旧活跃文件原地保留为历史文件，供 `coupling_count` 控制的下次摘要引用。
