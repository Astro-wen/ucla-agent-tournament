# AGENTS.md（给 Codex 和其他编码 agent）

这个仓库的规则写在 `CLAUDE.md`，**对你同样适用**（把里面的 "Claude" 读成「你」）。先读它，再读当前任务文档：

- **当前任务（2026-10-16）：王者荣耀 5v5 策略迭代** → `docs/CODEX_HOK.md`

最要紧的几条（细节以 `CLAUDE.md` 和任务文档为准）：

1. 提交只署主人名字 `Aaron Wen <siyuanwe@g.ucla.edu>`，不加任何 AI 署名（Claude、Codex、Co-Authored-By 都不加）。
2. 不提交 `.env*`、`logs/*.out`、临时日志；不打印密钥。
3. 不登录网站、不输密码、不创建账号；平台上需要登录的操作由主人做。
4. 比赛 agent 在主人电脑上从 main 的 checkout 运行，每局新开进程读 `agent/`：**合进 main 就等于上线**。只有任务文档写明的窗口内、过了检查才能合。
5. `altruagent/` 是官方 SDK，不改。
6. 和主人说话用简单的中文，术语要解释。
