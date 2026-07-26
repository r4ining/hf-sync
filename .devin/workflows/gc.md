---
description: Git commit - 暂存所有更改并生成 Conventional Commits 格式的提交
---

# /gc — Git Commit Workflow

执行以下步骤来完成一次 git commit：

1. 运行 `git status` 查看当前工作区的改动状态（包括暂存和未暂存的文件）。

2. 运行 `git diff` 和 `git diff --cached` 查看未暂存和已暂存的具体改动内容，理解改动的意图和范围。

3. 根据改动内容，判断 commit 类型（从以下选择）：
   - `feat`: 新功能
   - `fix`: 修复 bug
   - `docs`: 文档变更
   - `style`: 代码格式调整（不影响功能）
   - `refactor`: 重构（非新功能、非修 bug）
   - `perf`: 性能优化
   - `test`: 测试相关
   - `chore`: 构建/工具/依赖等杂项

4. 运行 `git add -A` 暂存所有更改。
   // turbo

5. 生成简洁的 commit message，遵循 Conventional Commits 格式：
   ```
   <type>: <简短描述>
   ```
   - 描述用中文，简洁明了地概括改动内容
   - 如果改动较复杂，可在描述后空一行添加正文（body），详细说明改动原因和内容

6. 运行 `git commit -m "<commit message>"` 完成提交。
   // turbo

7. 运行 `git log --oneline -1` 确认提交成功，并向用户展示提交结果。
