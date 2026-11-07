# 安装与本地导入

## 两件不同的事

安装 `skills/skill-engine` 是让 Codex 发现这个对话工作流；连接本地目录是让编辑器索引你已有的技能。单独安装 SKILL.md 不会获得浏览器任意读盘能力，也不会自动启动服务或上传技能。

当前官方 Codex 用户目录为 `~/.agents/skills`，仓库可使用 `.agents/skills`。本项目现有后端默认仍是 `~/.codex/skills`；请先确认实际目录再连接。官方发现机制与维护工具的可编辑范围不是同一件事。[官方说明](https://learn.chatgpt.com/docs/build-skills)

手动安装时保留完整的 `skill-engine/`、`agents/` 和 `references/` 结构。若目标已有同名技能，先比较其版本，不能用安装命令无提示覆盖。私有 GitHub 仓库需要有权限的下载或已认证的安装器。

## 现有默认路径

在仓库根启动：

```sh
python3 -m skill_editor.server --port 8766
```

默认个人/系统发现结构：

```text
~/.codex/skills/
  example-skill/SKILL.md
  .system/example-system-skill/SKILL.md
```

插件根默认为 `~/.codex/plugins/cache`。后端只监听回环地址；页面从同一服务加载，没有让 GitHub 托管页读取你的文件。

打开 `http://127.0.0.1:8766/`，点击“重新扫描技能文件”。首次扫描创建本地 `data/inventory.json`；后续扫描保留旧版本与变化摘要。React Flow 体验页先构建，再访问 `/flow-lab/index.html` 并重读已有目录。

## 指定另一套本地目录

当前 `--project` 指应用项目根，不是技能目录。CLI 没有 `--personal-root`，不要传一个不存在的参数。可以通过已有 Python 接口，用同一个 store 指定实际根并提供 HTTP 服务：

```sh
python3 - <<'PY'
from pathlib import Path
from http.server import ThreadingHTTPServer
from skill_editor.core import SkillStore
from skill_editor.server import make_handler

# 改为你实际使用的目录；这里是当前官方用户目录示例。
skills_root = Path.home() / '.agents/skills'
store = SkillStore(Path.cwd(), personal_root=skills_root)
server = ThreadingHTTPServer(('127.0.0.1', 8766), make_handler(store))
print('Skill Engine: http://127.0.0.1:8766', flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
finally:
    server.server_close()
PY
```

选择已有的、未被其他应用占用的端口，不中断其他服务。启动后仍由用户点击显式扫描；不要扫描整盘。首次连接其他根使用新的干净项目副本，避免把两位用户的旧目录合在一起。本项目当前拒绝符号链接并保守处理不可读取文件；这比 Codex 自身的发现规则更窄。

## 索引包含什么

读取文件名、name、description、正文、路径、来源和内容 SHA；不执行技能说明或脚本，不把整套资源注入对话。技能原文件及资源继续保留在原目录，选中后按需读取。系统技能和插件保持只读。

新扫到的插件缓存进入 `cachedSkills`，不能据此宣称插件已启用。当前会话暴露目录需由 Codex 会话另行核对；扫描不会替换或刷新该目录。用途标签不改变 runtime SKILL.md。

## 本地状态和上传

本地生成的 `data/*.json`、`.state/`、`.skill-editor/` 和 `web/inventory.js` 被忽略；不要 force-add 它们。原始技能库也不应放进应用仓库。当前发布包没有个人技能全文、作者目的草稿或历史案例。

只读浏览不需要模型 API。只有明确使用网页的可选模型分析时，选定内容才会按界面说明交给 OpenRouter；当前 Codex 对话与网页 API 是两条不同路径。
