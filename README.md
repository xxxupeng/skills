# Skills

可独立安装的 Codex skills，每个子目录是一个完整 skill。

## scheduled-autonomy：定时目标推进

[Skill 入口](scheduled-autonomy/SKILL.md) · [操作与边界](scheduled-autonomy/references/operations.md)

在用户授权的目标和时间窗口内推进工作；等待长任务时结束模型推理，由宿主机后台计时器唤醒原会话。
不启动原生 active goal，不创建新会话，也不在等待期间持续调用模型。

当前后端需要 Linux、systemd 用户服务、Python 3、`websocket-client`，以及当前用户可访问的 Codex app-server control socket。
将 `scheduled-autonomy/` 完整目录安装到 `$CODEX_HOME/skills/`（未设置时为 `~/.codex/skills/`），已有同名目录时先比较，不直接覆盖。
安装本身不会启动目标；使用前阅读操作文档，核对当前会话与宿主环境并执行 `probe`。

示例请求：

> 使用 $scheduled-autonomy，开启 8 小时窗口。目标是……；验收标准是……；允许的资源和操作是……；禁止……。
> 等待训练时至少间隔一小时检查，同一会话只保留一个待执行唤醒，到期不再启动新探索。

### 验证与限制

```bash
python3 -m unittest discover -s scheduled-autonomy/tests -v
```

测试使用隔离状态和模拟服务，不发送真实会话消息。2026-09-14 已在 Linux 远程宿主验证：桌面 App 关闭期间，后台唤醒原会话、执行宿主本地命令，并收到本轮完成事件。
这不代表所有桌面工具都能离线使用；长时间会话卸载后的恢复尚未完成真实离线验证，机器重启或 app-server 退出后的自动恢复也不保证。
告警默认保存在宿主状态文件及 journal，本机通知不等于可靠的远程通知。

目标、会话状态、日志和锁文件存储在运行宿主，不应提交到本仓库。

## License

[MIT](LICENSE)
