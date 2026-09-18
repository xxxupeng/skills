"""Local, explicit-ID Codex exec transport. No server, shell, fork or new thread."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import uuid


def session_state(path):
    meta, context, active = None, None, False
    with path.open() as stream:
        for line in stream:
            # A partial concurrent write is not evidence that the session is idle.
            row = json.loads(line)
            payload = row.get('payload', {})
            if row.get('type') == 'session_meta':
                meta = payload
            elif row.get('type') == 'turn_context':
                context = payload
            elif row.get('type') == 'event_msg':
                kind = payload.get('type')
                if kind == 'task_started':
                    active = True
                elif kind in ('task_complete', 'task_aborted', 'turn_aborted'):
                    active = False
    if not meta or not context:
        raise ValueError('CLI session metadata/turn context missing')
    return meta, context, active


def has_writer(path):
    """Linux same-user rollout writers, including an idle TUI. No process signals."""
    target = path.stat()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            if proc.name != str(os.getpid()) and 'codex' not in (proc / 'comm').read_text().lower():
                continue
            for fd in (proc / 'fd').iterdir():
                try:
                    st = fd.stat()
                    if (st.st_dev, st.st_ino) != (target.st_dev, target.st_ino):
                        continue
                    info = (proc / 'fdinfo' / fd.name).read_text()
                    flags = next(int(line.split()[1], 8) for line in info.splitlines()
                                 if line.startswith('flags:'))
                    if flags & os.O_ACCMODE != os.O_RDONLY:
                        return True
                except (FileNotFoundError, ProcessLookupError):
                    continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        # PermissionError intentionally propagates: cannot prove exclusive ownership.
    return False


class CLI:
    backend = 'cli'

    def __init__(self, state, log_dir):
        self.state = state
        self.thread = str(uuid.UUID(state['thread_id']))
        self.home = Path(state['codex_home']).resolve()
        self.log_dir = Path(log_dir)
        self.process = None
        self.stdout = self.stderr = None
        candidates = list((self.home / 'sessions').rglob('*' + self.thread + '*.jsonl'))
        if len(candidates) != 1:
            raise ValueError('CLI requires exactly one local unarchived rollout for explicit ID')
        self.path = candidates[0]
        self.binary = shutil.which(state.get('codex_binary') or 'codex')
        if not self.binary:
            raise ValueError('Codex CLI executable unavailable')
        self.identity = self.check_identity()
        if state.get('cli_identity') and self.identity != state['cli_identity']:
            raise ValueError('CLI model/permissions/config identity changed; rebind explicitly')

    def check_identity(self):
        meta, ctx, _ = session_state(self.path)
        cwd = str(Path(self.state['cwd']).resolve())
        if (meta['id'] != self.thread or str(Path(meta['cwd']).resolve()) != cwd
                or str(Path(ctx['cwd']).resolve()) != cwd
                or self.state['host'] != socket.gethostname()):
            raise ValueError('CLI thread/host/cwd identity mismatch')
        if meta.get('source') not in ('cli', 'exec') or meta.get('dynamic_tools'):
            raise ValueError('CLI fallback requires CLI/exec session without desktop dynamic tools')
        sandbox = ctx.get('sandbox_policy', {})
        if sandbox.get('type') not in ('read-only', 'workspace-write', 'danger-full-access'):
            raise ValueError('CLI unsupported sandbox; refuse permission changes')
        if not ctx.get('model') or not meta.get('model_provider'):
            raise ValueError('CLI original model/provider unknown')
        if ctx.get('approval_policy') != 'never':
            raise ValueError('CLI unattended wake requires an existing approval_policy=never session')
        allowed = {'type', 'writable_roots', 'network_access', 'exclude_tmpdir_env_var', 'exclude_slash_tmp'}
        if set(sandbox) - allowed:
            raise ValueError('CLI sandbox fields unsupported; refuse permission changes')
        # Hash config, never copy credentials into goal.json. User changes require rebind.
        h = hashlib.sha256()
        paths = sorted(set(self.home.glob('*.config.toml')) | {self.home/'config.toml'})
        for path in paths:
            h.update(path.name.encode())
            h.update(path.read_bytes() if path.exists() else b'<absent>')
        return dict(cwd=cwd, model=ctx['model'], provider=meta['model_provider'],
                    effort=ctx.get('effort'), sandbox=sandbox,
                    config_sha256=h.hexdigest(), rollout=str(self.path))

    def read(self, thread):
        if thread != self.thread or self.check_identity() != self.identity:
            raise ValueError('CLI identity changed before delivery')
        _, _, active = session_state(self.path)
        return {'id': thread, 'cwd': self.identity['cwd'],
                'status': {'type': 'active' if active or has_writer(self.path) else 'idle'}}

    def resume(self, thread):
        # exec resume is invoked only in start, after the durable sending claim.
        return self.read(thread)

    def command(self, thread):
        if thread != self.thread:
            raise ValueError('CLI ID mismatch')
        i = self.identity
        args = [self.binary, 'exec', '-C', i['cwd'], '-s', i['sandbox']['type'],
                '-c', 'approval_policy="never"', '-m', i['model'],
                '-c', 'model_provider=' + json.dumps(i['provider'])]
        if i['effort']:
            args += ['-c', 'model_reasoning_effort=' + json.dumps(i['effort'])]
        if i['sandbox']['type'] == 'workspace-write':
            for key in ('writable_roots', 'network_access', 'exclude_tmpdir_env_var', 'exclude_slash_tmp'):
                if key in i['sandbox']:
                    args += ['-c', 'sandbox_workspace_write.' + key + '=' + json.dumps(i['sandbox'][key])]
        return args + ['resume', '--json', thread, '-']

    def start(self, thread, prompt):
        if self.read(thread)['status']['type'] != 'idle':
            raise RuntimeError('CLI session became busy before start; no launch')
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attempt = 'cli-' + uuid.uuid4().hex
        self.out_path = self.log_dir / (self.attempt + '.jsonl')
        self.err_path = self.log_dir / (self.attempt + '.stderr')
        self.stdout = self.out_path.open('xb')
        self.stderr = self.err_path.open('xb')
        env = dict(os.environ, CODEX_HOME=str(self.home))
        # A fresh exec must acquire the explicit ID itself, not inherit a parent turn identity.
        env.pop('CODEX_THREAD_ID', None)
        env.pop('CODEX_TURN_ID', None)
        self.process = subprocess.Popen(self.command(thread), cwd=self.identity['cwd'], env=env,
                                        stdin=subprocess.PIPE, stdout=self.stdout, stderr=self.stderr,
                                        start_new_session=True)
        self.process.stdin.write(prompt.encode())
        self.process.stdin.close()
        return {'id': self.attempt, 'status': 'inProgress', 'pid': self.process.pid,
                'events': str(self.out_path), 'stderr': str(self.err_path)}

    def completion(self, thread, turn, timeout):
        if thread != self.thread or turn != self.attempt:
            raise ValueError('CLI completion identity mismatch')
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Never terminate a running agent on observation timeout or goal expiry.
            return {'status': 'completion_unconfirmed', 'error': 'CLI still running; do not redeliver'}
        started = False
        terminal = None
        try:
            with self.out_path.open() as events:
                for line in events:
                    row = json.loads(line)
                    if row.get('type') == 'thread.started':
                        if row.get('thread_id') != thread:
                            raise ValueError('CLI emitted different thread ID')
                        started = True
                    if row.get('type') in ('turn.completed', 'turn.failed'):
                        terminal = row['type']
        except (ValueError, OSError) as error:
            return {'status': 'completion_unconfirmed', 'error': str(error)}
        if code != 0 or terminal == 'turn.failed':
            return {'status': 'failed', 'error': f'CLI exit={code}; see {self.err_path}'}
        return {'status': 'completed' if started and terminal == 'turn.completed'
                else 'completion_unconfirmed'}

    def close(self):
        # Closing our copies never kills the child, which writes directly to files.
        for stream in (self.stdout, self.stderr):
            if stream:
                stream.close()
