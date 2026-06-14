## 配置项

| 配置项                          | 类型     | 默认值                          | 说明                                |
| ---------------------------- | ------ | ---------------------------- | --------------------------------- |
| `enable_groups`              | list   | `[]`                         | 监控的群号列表                           |
| `enable_private`             | bool   | `true`                       | 是否采集私聊                            |
| `forward_groups`             | list   | `[]`                         | 自动转发目标群号                          |
| `forward_private`            | list   | `[]`                         | 自动转发目标QQ号                         |
| `modality.text`              | bool   | `true`                       | 采集文本消息                            |
| `modality.image`             | bool   | `false`                      | 采集图片占位 `[图片]`                     |
| `modality.voice`             | bool   | `false`                      | 采集语音占位 `[语音]`                     |
| `summary_schedule`           | string | `08:00,20:00`                | 定时触发时间（HH:MM逗号分隔，留空关闭）            |
| `scheduled_summary_groups`   | string | `""`                         | 定时摘要黑名单（逗号分隔群号）                   |
| `coupling_count`             | int    | `0`                          | 定时摘要耦合历史文件数                       |
| `self_recursion`             | bool   | `false`                      | 是否采集机器人自身消息                       |
| `name_record`                | bool   | `true`                       | 是否记录 `[群昵称]`                      |
| `cache_clean_days`           | int    | `7`                          | 文件保留天数                            |
| `cache_clean_interval_hours` | int    | `12`                         | 清理检查间隔（小时）                        |
| `summary_prompt`             | text   | (见默认)                        | AI 摘要提示词模板                        |
| `summary_provider_id`        | string | `deepseek/deepseek-v4-flash` | LLM 模型 ID                         |
| `output_mode`                | string | `image`                      | 输出方式：`image` 或 `file`             |
| `suppress_notifications`     | bool   | `false`                      | 全局静默，禁止所有通知和来源标注                  |
| `style_path`                 | string | `""`                         | pillowmd 渲染风格目录，留空用内置 modern-dark |

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
