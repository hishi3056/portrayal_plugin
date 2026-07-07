# CHANGELOG

## v1.0.0 (2026-07-07)

### 里程碑
- 首个正式发布版本，双路画像注入（Planner + Replyer）

### 新增
- **Planner Hook 注入**：通过 `maisaka.planner.before_request` 替换/追加 Planner 中的画像，不动本体代码
- `enable_planner_injection` 配置项（默认关闭）
- 画像注入支持三种模式：仅 Replyer / 仅 Planner / 双路注入
- **版本"使用"按钮**：时间轴每项可激活旧版本为当前使用版本
- **词云双模式**：螺旋散排 + 网格平铺（水平/垂直90度随机两种走向）
- 关系图谱节点显示 QQ 头像（预加载后渲染）
- 连线 hover 显示双方信息（昵称+QQ+互动次数）
- 大节点下方显示群名片昵称
- Planner Hook 各分支 info 级别日志便于追踪

### 改进
- **话题分析独立于版本切换**：雷达图、关系图谱、词云始终用最新有数据的版本，不受查看/使用旧版本影响
- 跨群合并修复：`enable_cross_group_merge = false` 时严格不跨群
- 口头禅改为 ChatLab 式整条消息频率统计
- 前端词云兜底路径禁用画像文本分词（修复 LLM 输出标签污染）
- `enable_injection` 描述明确区分 Replyer 注入与 Planner 注入
- 时间轴按钮改为纯文字（查看/使用/删除）
- 当前版本标记改为紫色文字辨识
- 按钮宽度自适应

### 修复
- Planner Hook `_get_latest_sender_user_id` 消息结构解析路径错误（`message_info.user_info`）
- ECharts `fixed:true` / `label.formatter` 导致 canvas 不渲染
- 展示版/注入版切换失效（`findCachedProfile` 缓存未命中）
- 常规更新/阴阳更新无效果（缓存未命中静默失败）
- 更新后自动刷新用户详情
- 日期拼接 bug（msg_count 拼到年份前面）
- 配置 API 500（Pydantic `__dict__` 序列化失败）
- placeholder-area 重复 display 声明
- 跨群画像不检查 `enable_cross_group_merge` 配置

## v0.5.0 (2026-06-25)

### 新增
- WebUI 基础框架
- 版本历史与对比
- 回收站
- 生成日志
- 群分析统计

## v0.3.0 (2026-06-20)

### 新增
- 阴阳画像模式
- 保护名单与权限白名单
- 画像 TTL 与刷新提醒
- 图片展示版渲染

## v0.1.0 (2026-06-15)

- 初始版本
- 基础画像生成与 Hook 注入
- NapCat API 消息采集
- 独立 SQLite 存储
