# 群友观测簿 (portrayal_plugin)

基于 NapCat API 采集群聊消息，通过 LLM 生成用户画像，并注入 MaiSaka Planner/Replyer 作为低优先级回复参考。
目前功能初建并不完善，理论收益有，高不高未知，也有一些BUG，顺便征求好的内容建议。

## 功能

- **画像生成**：`+画像` / `+阴阳画像` 指令触发，分析目标用户的群聊发言。也可在webui中手动搜索生成用户画像或者更新已有画像。
- **两种画像模式**：常规画像（中性分析）/ 阴阳画像（毒舌风格）。可以自定义修改提示词，生成想要的内容。一种是展示内容，用于娱乐。一种是注入版，用于提供价值内容。
- **版本历史**：每次生成保存为新版本，可查看、对比、回滚
- **跨群隔离**：同一用户在不同群拥有独立画像，可配置跨群聚合
- **六维雷达图**：水群力、群影响力、社交力（规则统计零 token）+ 输出力、整活值、知识值（LLM 评分）
- **关系图谱**：ECharts 力导向图展示用户间的互动关系，节点显示 QQ 头像
- **词云分析**：jieba 分词 + 词性过滤 + 径向词云
- **WebUI 管理**：三栏布局，画像/日志/分析/Prompt/回收站/配置六个标签页  内容并没有完善
- **双路注入**：Planner Hook 替换原生画像 + Replyer Hook 覆盖回复参考  Replyer是自身魔改才可以使用。


## 指令

| 指令 | 说明 |
|------|------|
| `+画像` | 分析自己的画像 |
| `+画像 @某人` | 分析目标用户的画像 |
| `+画像 某人名字` | 按名字分析目标用户 |
| `+阴阳画像` | 毒舌风格分析自己 |
| `+阴阳画像 @某人` | 毒舌风格分析目标用户 |
| `+查看画像` | 查看已生成的画像 |

命令前缀可通过 `config.toml` 中的 `command_prefix` 自定义。

## 前提条件

| 条件 | 必需 | 说明 |
|------|------|------|
| MaiBot 1.0.6+ | 是 | 需要 MaiSaka 已合并的版本 |
| NapCat | 是 | 消息采集通过 NapCat OneBot API |
| jieba | 推荐 | 中文分词与词性标注，不装则回退正则匹配 |
| LLM API Key | 是 | 画像生成需要调用 LLM（默认 DeepSeek） |

## 安装

1. 将 `portrayal/` 目录复制到 MaiBot 的 `plugins/` 文件夹
2. 编辑 `config.toml` 填写 LLM API Key
3. 重启 MaiBot

## 配置

编辑 `config.toml`：

```toml
[portrayal]
# ─── 消息采集 ───
scan_hours = 168           # 采集时间范围（小时），默认7天
message_limit = 100        # 目标用户发言采集上限
min_target_messages = 3   # 最低发言条数，低于此值拒绝生成

# ─── LLM 配置 ───
provider = "deepseek"             # deepseek 或 maibot
deepseek_api_key = ""             # DeepSeek API Key
deepseek_base_url = "https://api.deepseek.com/v1"
deepseek_model = "deepseek-chat"
temperature = 0.7
chat_log_max_chars = 8000         # 聊天记录截断字符数

# ─── 画像注入 ───
# 是否通过 Hook 注入画像到 Replyer（需 MaiSaka 补丁或 MaiBot 1.0.6+）
# 关闭后不影响画像采集、WebUI 等其他功能
# 原版 Planner 注入由 MaiBot 全局配置控制，与本开关无关
enable_injection = true

# 是否通过 Hook 注入画像到 Planner，替换原版 A_memorix 画像（需 MaiBot 1.0.6+）
# 默认关闭；开启后建议在 MaiBot WebUI 配置→记忆→聊天中使用记忆→自动注入人物画像 中关闭
enable_planner_injection = false

# 跨群画像聚合：多群画像合并注入
enable_cross_group_merge = true

# ─── WebUI ───
webui_port = 8089
webui_host = "127.0.0.1"          # 0.0.0.0 = 局域网可访问
webui_token = ""                  # 非本机绑定时强烈建议设置

# ─── 权限 ───
command_prefix = "+"
protected_user_ids = []          # 保护名单，不允许被分析
allowed_user_ids = []            # 权限白名单，空=所有人可用
```

## 画像注入说明

本插件有两条独立的画像注入路径，与原版 MaiBot 的注入路径互不冲突：

| 注入路径 | 控制开关 | 位置 | 数据源 |
|---------|---------|------|--------|
| **Replyer 注入** | 插件配置 `enable_injection` | `maisaka.replyer.before_request` Hook | 插件 SQLite 画像 |
| **Planner 注入** | 插件配置 `enable_planner_injection` | `maisaka.planner.before_request` Hook | 插件 SQLite 画像 |
| **原版 Planner 注入** | MaiBot 全局配置 `enable_person_profile_injection` | `A_memorix` | MaiBot 记忆系统 |

### Planner 注入机制

插件通过 `maisaka.planner.before_request` Hook 在 Planner 发起请求前介入：
1. 遍历 messages 列表，查找 `【人物画像-内部参考】` 开头的原生画像消息
2. 找到 → 替换内容为插件画像
3. 没找到（原版没注入）→ 追加一条新消息
4. 不动本体代码，纯 Hook 机制

### 推荐配置

- 只用 Replyer 注入：`enable_injection=true, enable_planner_injection=false` *此为魔改内容使其Replyer可以注入画像。
- Replyer + Planner 都用插件画像：两个都开 `true`，并在 MaiBot WebUI 关掉原生 `自动注入人物画像` 以避免重复查询
- 全关：两个都设 `false`，画像仍可采集和查看，不注入

### 跨群画像

- `enable_cross_group_merge = true`：当前群没有画像时，使用其他群的画像（标注来源群）
- `enable_cross_group_merge = false`：严格隔离，只在当前群画像可用时注入

## 待更新

后续更新内容：
1. 优化关系算法。
2. 优化提示词，喜欢的游戏、话题、活跃时间、别名。
3. 加入自然记录采集。
4. 群分析。
5. 画像图片美化。
6. 验证完收益决定更新。


## 资源占用

- **存储**：~7 MB（含数据库 + 头像缓存），消息不持久化
- **内存**：~25 MB（含 jieba 词典），消息缓存 10 分钟自动清除
- **CPU**：零空闲开销，纯指令触发
- **网络**：每次采集 ~200KB + LLM 调用

## 效果图
 





## 文件结构

```
portrayal/
├── _manifest.json              # 插件清单（manifest v2）
├── plugin.py                   # 插件入口
├── config.toml                 # 插件配置
├── store.py                    # 独立 SQLite 存储
├── napcat_collector.py          # NapCat API 消息采集
├── web_server.py               # WebUI HTTP 服务器
├── group_analyzer.py           # 群分析统计
├── prompts/                    # LLM 提示词模板
│   ├── portrait_system.txt
│   ├── portrait_user.txt
│   ├── yin_yang_system.txt
│   └── yin_yang_user.txt
├── webui/dist/                 # WebUI 前端
│   ├── index.html
│   ├── echarts.min.js
│   └── echarts-wordcloud.min.js
├── LICENSE
└── README.md
```

## 鸣谢
本插件开发过程中，参考并复用了以下开源项目的实现思路与代码片段：
- 群用户画像分析思路：https://github.com/Zhalslar/astrbot_plugin_portrayal
- UI美术风格、话题分词统计思路：https://github.com/ChatLab/ChatLab


## License

GPL-3.0
