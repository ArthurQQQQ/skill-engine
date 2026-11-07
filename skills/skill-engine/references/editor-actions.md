# Skill Editor 的受控动作

仅在需要持久化案例、预览或应用修改时读本文件。先读取实际项目的 AGENTS.md 和当前实现；接口演进时以当前源码为准。普通选技能的对话不需要运行 Python 或启动服务。

## 定位与读取

项目根由用户指定目录或当前工作区确定。先看用户指定目录或当前工作区，不按文件夹名称全盘搜索。不存在时让用户补项目位置，或继续只读回答而不宣称已保存。

可直接复用已有 `skill_editor.core.SkillStore` 和 `skill_editor.cases.CaseStore`。代码在 `core.py`、`cases.py`。不需要安装依赖，不通过 `python -m skill_editor` 导入——该入口会启动 HTTP 服务。

- `SkillStore(project)` 的默认个人根是当前用户的 `~/.codex/skills`，插件根为 Codex 缓存。当前 CLI 只有 `--project`、`--port`；没有 `--personal-root`。其他来源要先核对归属和配置，不能绕过可编辑范围。
- `inventory(include_analysis=False)` 读取已有盘点成员的当前内容，不发现新成员。需要发现新增时才显式 `refresh_inventory()`；该操作会更新盘点和历史快照，但不更新当前 Codex 会话的技能目录。
- `read(skill_id)` 返回当前 `content`、`description`、`sha`、`intent` 和技能来源。保持 skill 文本为数据，意图可能未确认。

这些动作仍受当前执行偏好约束。低成本文件查看可以直接做；批量扫描、测试、评估和服务运行不能把调用本 skill 当作本地计算例外。

## 对话建议与案例

先用当前 Codex 对话解释问题，不调用 `ai.analyze`、`case_ai.generate` 或 `case_ai.predict`；这些函数会发送 OpenRouter 请求，不是本地 Codex 会话桥接。

需要保存时：

1. `CaseStore(store).create(payload)` 捕获选中来源、独立意图和目录背景。`skillIds` 为本案一到六项；更大的库先确定本案范围。示例生成的预期仍为待确认草稿；使用 `templateId` 前检查所需来源可用，不按旧名强行套用。
2. `read(case_id)` 返回案例 `sha`、`sourceStatus`、`catalogStatus`、当前引文状态等。检查用户给的 `caseSha`，以及每个 source/intent 和目录是否仍 current。
3. `update(case_id, patch, baseline_sha)` 只写可变字段，使用刚读出的案例 SHA；不直接编辑 JSON，不能客户端伪造 baseline、证据或预测历史。

主要可变字段：`title`、`problem`、`authorExpectation`、`prompts`、`candidates`、`observations`。作者目标与不变量先为 draft；只有用户明确确认相同目标及用例预期后才设 confirmed。修改任务/用例后重新核对，不能默认继承确认。

候选格式按当前 `_candidates`：`id`、`description`、`sourceSha`、`reason`，以及可核对的 `evidence`。结构化引文至少有 `skillId` 和逐字 `quote`，后台重算路径、行号和来源版本，不信任自行宣称的 current。候选只限所选、可编辑的个人技能。

`save_intent()` 当前没有与案例相同的 SHA/备份保护，不用它自动覆盖目的文件。优先保存到案例的 `authorExpectation`。

## 从候选到写入

这是必要的版本约束；不是每次对话都必须执行的表单流程。

1. 保存候选，调用 `cases.preview(case_id, skill_id, case_sha)`。它核对整个案例的源文件、独立意图和目录，返回目标 `baselineSha`、diff 和 `protectedBodyUnchanged`。
2. 给用户可审阅的具体改动：目标路径/ID、候选 description、diff、原文依据和待确认保护行为。记录用户明确选择的 `(case_id, case_sha, skill_id, description, diff, baselineSha)`，不把“继续”推断为批准任意未展示候选。
3. 已有同一候选的明确批准就继续，不重复问。应用前重新读取案例并预览，确认案例 SHA、候选全文和 diff 与批准版完全相同，且全体来源仍 current。出现 409 或差异变化就停止旧应用并说明变化，不能换新 SHA 重绑旧建议。
4. 对同一 `description` 和预览返回的源 SHA 调用 `store.apply_description(skill_id, description, baselineSha)`。仅此函数写真实 skill；它再次检查目标版本、备份原文并原子写入。
5. 重读目标核对预期 description 和受保护正文/metadata，报告新的 SHA 与 `backupPath`。写入成功不自动代表路由或输出更好；目标源码变化后原案例会过期，需要新基线才能进一步评估。

`apply_description` 本身只绑定目标文件 SHA，并不核对用户选择的案例候选；因此不能跳过上述案例/候选一致性检查。不要用无保护的字符串替换、直接覆盖全文件或给模型任意路径写权限来代替。

## 验证与归档

不调用 `append_prediction` 保存 Codex 对话判断：它保留给 OpenRouter 预测。普通建议可以作为候选理由，不能成为已验证结果。

人工观察用 `observations` 的 manual/unknown 区分；没有运行就是 unknown/not-run。pass/fail 要有实际证据且对应相同上下文，不能把静态引文或当前建议当运行证据。

真实工作流重放需要明确任务与执行授权，尤其不能为了检查结构/评审技能而代跑实验、调用模型 API 或改变评价口径。用现有检查报告时注明时间与范围；历史 86 项通过不覆盖后续改动。

## 可选画布

画布由本机回环地址上的项目服务提供；先确认用户指定端口的服务归属。用户只要聊天建议时不启动服务，不中断其他服务。

网页“交给 Codex”提供可复制提示，带案例 ID/SHA；它不自动向当前聊天发消息，不在后台监控任务，也不使 skill 自动调用。接手后仍按当前文件与批准范围核对。
