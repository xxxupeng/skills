# Skills

可独立安装的 Codex skills，每个子目录是一个完整 skill。

## scheduled-autonomy：定时目标推进

[Skill 入口](scheduled-autonomy/SKILL.md) · [操作与边界](scheduled-autonomy/references/operations.md)

在用户授权的目标和时间窗口内推进工作；等待长任务时结束模型推理，由宿主机后台计时器唤醒原会话。
不启动原生 active goal，不创建新会话，也不在等待期间持续调用模型。

当前需要 Linux、systemd 用户服务和 Python 3。新目标默认自动选择后端：优先可访问的 Codex app-server
（需要 `websocket-client`）；没有可连接 socket 时，可对已核验的纯 CLI 会话使用 `codex exec resume <明确UUID>`，
无需运行 app-server。也可通过 `--backend app-server` 或 `--backend cli` 固定后端。
带桌面动态工具的 App 会话不静默转为 CLI；CLI 会话需已有非交互权限配置，并在唤醒前退出交互终端、释放会话。
将 `scheduled-autonomy/` 完整目录安装到 `$CODEX_HOME/skills/`（未设置时为 `~/.codex/skills/`），已有同名目录时先比较，不直接覆盖。
安装本身不会启动目标；使用前阅读操作文档，核对当前会话与宿主环境并执行 `probe`。

示例请求：

> 使用 $scheduled-autonomy，开启 8 小时窗口。目标是……；验收标准是……；允许的资源和操作是……；禁止……。
> 等待训练时至少间隔一小时检查，同一会话只保留一个待执行唤醒，到期不再启动新探索。

### 验证与限制

```bash
python3 -m unittest discover -s scheduled-autonomy/tests -v
```

测试使用隔离状态、模拟服务与本地 CLI 子进程 fixture，不调用模型。2026-09-18 双后端版本通过 47 项测试；
真实模型的纯 CLI 延迟唤醒尚待端到端验收。2026-09-14 已在 Linux 远程宿主验证 app-server 路径：桌面 App 关闭期间，后台唤醒原会话、执行宿主本地命令，并收到本轮完成事件。
这不代表所有桌面工具都能离线使用；长时间会话卸载后的恢复尚未完成真实离线验证，机器重启或 app-server 退出后的自动恢复也不保证。
告警默认保存在宿主状态文件及 journal，本机通知不等于可靠的远程通知。

目标、会话状态、日志和锁文件存储在运行宿主，不应提交到本仓库。

## License

[MIT](LICENSE)
