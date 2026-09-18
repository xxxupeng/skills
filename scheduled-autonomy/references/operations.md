# 操作与边界

## 环境与身份

本版本后端：Linux、`systemd --user`、Python3。
app-server 投递需要 `websocket-client`；CLI 投递只需要同宿主已安装且已登录的 Codex CLI。
不自动安装依赖、重启服务或公开网络端口。

入口为本 Skill 目录的 `scripts/scheduler.py`，下文 `S` 指该脚本绝对路径。
使用同一个 Python 解释器（如 `/usr/bin/python3`）执行与部署；采用 app-server 时确保它能导入 `websocket`。
默认 Skill 安装在当前用户 Codex skills 目录；多个机器需要各自在其宿主安装，不跨机猜路径。

默认 thread ID 来自 `CODEX_THREAD_ID`，有该环境变量时禁止显式指向另一个 ID。
默认 socket 来自当前 `CODEX_HOME`（未设置则用户 `.codex`）的 `app-server-control/app-server-control.sock`。
若宿主实际 socket 不同，在 `init --socket` 指定已核实的同用户 socket。
`init --backend auto` 为新目标默认模式：先对现有服务执行 initialize + thread/read，并核对 UUID/cwd。
只有 socket 不存在或明确拒绝连接时才尝试 CLI；超时、身份错误、忙碌或发送不明不触发后端切换。
CLI 从同一 CODEX_HOME/sessions 下查找唯一 UUID rollout，核对宿主/cwd/source/最近 turn_context。
初始化与 probe 均不创建/恢复会话、不调用模型；到点投递才恢复同一 ID。
多会话可以共享一个服务，但每个会话有自己的 UUID 状态目录和 worker；绝不硬编码示例 UUID。

默认状态：`$CODEX_HOME/scheduled-autonomy/<threadId>/goal.json`（未设置时为用户 `.codex` 下）。
状态在运行机器落盘，不进入项目 Git，不把凭据写进 prompt。`--root` 只用于明确需要的状态位置或隔离测试。
命令全局参数（`--root` / `--thread`）放在子命令之前。

## 命令

创建前必须有明确目标、验收、权限、截止时间，且原生 goal 已核对 inactive。
`--native-goal-inactive` 是操作者确认记录，不是脚本自动关闭或证明原生 goal。

```bash
python3 "$S" init --objective '获批目标' --criteria '固定验收标准' \
  --authorization '用户批准的范围、资源与禁止项；批准消息/文档引用' \
  --deadline '2026-10-01T12:00:00+08:00' --min-interval 3600 --backend auto --native-goal-inactive
python3 "$S" probe
python3 "$S" progress --summary '证据和当前结果；固定总时长及检查计数' --next-action '当前实验与下一判断'
python3 "$S" schedule --at '2026-10-01T10:00:00+08:00' --prompt '核对指定实验。未完成且正常则安排下一次检查后结束；完成则分析记录并在既有授权内推进；异常则定向诊断。'
python3 "$S" status
```

日期是语法示例，执行时用实际绝对时间，必须含时区。脚本输出时间戳是 Unix seconds，显示给用户时换成本地时间。
`--min-interval` 在初始化时按已知任务类型/用户约束确定；训练取 max(3600, 固定预计总时长/10)。
若任务时长后来增加，在下一次 `--at` 选择更长间隔，不必改合同；降低频率约束需明确记录。

已有 pending 时默认拒绝第二个 schedule，避免重复定时器；确需修改时间或 prompt 时加 `--replace`。
普通 `progress` 不清空 pending；改变目标合同 `revise` 会清空旧 pending，需按新合同重新安排。
重复 `init` 被拒绝，不自动覆盖旧目标或重新开始24小时。

```bash
python3 "$S" pause --reason '用户暂停'
python3 "$S" resume --reason '用户明确恢复；原截止时间不变'
python3 "$S" revise --deadline '2026-10-02T12:00:00+08:00' --authorization-note '用户明确延长至该时间的消息引用'
python3 "$S" wait-user --reason '具体阻塞及所需决定'
python3 "$S" complete --reason '验收结果与证据路径'
python3 "$S" cancel --reason '用户取消'
```

`revise` 支持 objective/criteria/authorization/deadline/min-interval；保留变更前后与授权记录。
恢复不立即发消息；安排新 schedule 才启动后台服务。暂停/取消/完成会清空 pending，worker 最迟约一分钟退出。
已经 accepted 的轮次无法靠取消 pending 撤回；脚本从不调用 turn/interrupt。

## 后端选择与 CLI 边界

- `--backend auto`：自动选择安全可用的当前会话后端；`app-server` / `cli` 可固定后端。
- `--codex-binary /绝对路径/codex`：需要时指定 CLI，初始化保存实际可执行路径，避免 systemd PATH 不同。
- 旧目标无 backend 字段时保持 app-server；升级 Skill 不重置目标、时间预算或 pending。
- App 会话（尤其含 dynamic_tools）不伪装成纯 CLI 会话。App socket断开时没有已绑定且兼容的CLI身份就等待协调。
- CLI 当前支持 source=cli/exec、无 dynamic_tools、可验证的 read-only/workspace-write/danger-full-access 沙盒和
  已有 approval_policy=never。需审批的会话不自动修改权限以求无人值守；不添加 bypass、ignore-rules 或 ignore-user-config。
- 固定原模型/provider/effort、cwd/CODEX_HOME、沙盒参数，保存配置指纹；配置或会话身份变化后拒绝静默恢复。
  自定义未适配的沙盒字段会拒绝执行，不猜等价权限。
- CLI 不要求桌面/App在线，但交互式 Codex 必须释放这个会话。未结束 task_started 或同用户 Codex 的可写 rollout
  文件描述符表示 busy；最多延后一小时/截止时间，不调用模型试探，也不杀终端。占用检查不提供跨产品原子锁，
  后台投递期间用户不能同时手动 resume 同一 ID。
- CLI命令通过 argv/标准输入投递，不拼 shell；不使用 --last、fork、ephemeral，也不新建会话。
  唤醒轮次仍需本地Skill/项目指令/MCP和有效登录；不能假定桌面动态工具在CLI里存在。

CLI 完成证据位于状态目录 `deliveries/cli-*.jsonl`、`*.stderr`；last_delivery记录 backend/PID/路径。
必须读到匹配 UUID 的 thread.started、turn.completed，且子进程退出0才算 completed；仅 spawn/退出0都不足。
失败或未确认时清除后继并等待协调，不跨后端重复发送。30分钟/截止时间只限制观察，不强杀已开始轮次。
worker使用 KillMode=process，避免观察器退出时 systemd 连带杀死 CLI 子进程；这也意味着取消计时器不终止已启动的代理。

## 后台运行

`schedule` 在已有worker正观察accepted轮次时复用它，防止本轮结束前安排后继切断完成订阅；其余情况串行重启自己的计时器，绝不重启 app-server。
worker 每次最多睡60秒以感知取消/更新（纯操作系统等待，不调用模型、不查询训练）。
未到期时不连接 app-server/启动CLI；到期若会话 active，每10分钟重查一次，最多一小时/目标截止时间。
到点记录原始状态；idle/notLoaded通过当前服务thread/resume原ID（不覆盖model/config/dynamicTools），再核对身份和截止；active延后。
systemError/恢复失败/传输失败每10分钟有限重试，最多一小时/截止时间；失败留原始原因与告警。
到期只调用一次 `turn/start`，不传 model/权限覆盖，不使用 steer/interrupt。保持订阅至turn/completed或最多30分钟/截止。
完成状态保存到last_delivery；完成但未安排后继且未明确结束目标则waiting_user并告警。
结果不明清除后继并等待协调，不重发；禁止从一个后端发送失败后用另一个后端再投同一条。

查看实际服务和日志：

```bash
systemctl --user status "codex-scheduled-autonomy-当前UUID.service"
journalctl --user -u "codex-scheduled-autonomy-当前UUID.service" -n 20 --no-pager
```

状态锁串行化 schedule/cancel/send；worker 锁避免重复守护进程。
发送前原子记录 sending；结果不明或进程崩溃留下 sending，后续不自动重发，需核对会话实际历史后决定。
这提供“至多一次发送尝试”，不是网络 exactly-once 保证；宁可漏一次待人工协调，不重复行动。
告警写入同状态目录ALERT.json和journal，尝试notify-send并保存其结果；等待用户故障退出码为非零。
本机桌面通知不能等同跨SSH通知用户。没有可用通知服务时明确记录unavailable；尚未实现独立外部可靠通知通道。
终端断连不影响 systemd 用户服务；用户退出/机器重启可能影响服务，取决于宿主配置。
不会自行启用 linger 或开机自启；重新上线后 status + probe，再核对旧投递是否已执行。

## 验证

`python3 -m unittest discover -s <skill>/tests -v` 使用隔离临时状态、Unix/WebSocket peer与本地CLI子进程fixture，不调用模型。
`probe` 对现有 app-server/本地CLI会话只读，输出实际 backend。真实首次投递建议只要求确认收到，禁止启动实验。
子进程fixture不等于真实模型唤醒验收；新主机还需验证登录、版本、会话恢复与一条无害回复。
来源：[官方 App Server 协议](https://developers.openai.com/zh-Hans/docs/app-server)、
[官方非交互 CLI 与 exec resume](https://developers.openai.com/zh-Hans/docs/non-interactive-mode)。
当前实现使用 Unix socket 上的 WebSocket，不是给 `codex app-server proxy` 写裸 JSONL。
