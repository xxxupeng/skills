#!/usr/bin/env python3
"""Linux current-thread scheduler. No native goal, new server, or new thread."""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid

STOPPED = {'paused', 'cancelled', 'completed', 'expired', 'waiting_user'}


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, root, thread):
        self.thread = str(uuid.UUID(thread))
        self.folder = Path(root).expanduser().resolve() / self.thread
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.folder / 'goal.json'
        self.lock = self.folder / 'state.lock'

    @contextlib.contextmanager
    def edit(self):
        with self.lock.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            s = json.loads(self.path.read_text()) if self.path.exists() else {}
            yield s
            atomic_json(self.path, s)

    def read(self):
        return json.loads(self.path.read_text())

    def initialize(self, *, objective, criteria, authorization, cwd, socket_path,
                   deadline, min_interval=3600, now=None):
        now = time.time() if now is None else now
        if not all(str(x).strip() for x in (objective, criteria, authorization)):
            raise ValueError('objective, criteria and authorization required')
        if not math.isfinite(deadline) or deadline <= now or min_interval <= 0:
            raise ValueError('future deadline and positive interval required')
        with self.edit() as s:
            if s:
                raise ValueError('goal already exists; use revise/resume, never reset budget')
            s.update(schema=1, thread_id=self.thread, host=socket.gethostname(),
                     cwd=str(Path(cwd).resolve()), socket=str(Path(socket_path).expanduser().resolve()),
                     objective=objective, criteria=criteria, authorization=authorization,
                     created_at=now, deadline=deadline, min_interval=min_interval,
                     status='active', progress='', next_action='', pending=None,
                     last_delivery=None, revisions=[], updated_at=now)

    def progress(self, progress, next_action, now=None):
        with self.edit() as s:
            s.update(progress=progress, next_action=next_action,
                     updated_at=time.time() if now is None else now)

    def revise(self, changes, *, authorization_note, now=None):
        if not authorization_note.strip():
            raise ValueError('explicit user authorization record required')
        if not changes or set(changes) - {'objective', 'criteria', 'authorization', 'deadline', 'min_interval'}:
            raise ValueError('invalid revision fields')
        now = time.time() if now is None else now
        if 'deadline' in changes and (not math.isfinite(changes['deadline']) or changes['deadline'] <= now):
            raise ValueError('deadline must be in future')
        if 'min_interval' in changes and changes['min_interval'] <= 0:
            raise ValueError('interval must be positive')
        with self.edit() as s:
            s['revisions'].append({'at': now, 'authorization': authorization_note,
                                  'before': {k: s[k] for k in changes}, 'after': changes})
            s.update(changes); s.update(updated_at=now, pending=None)

    def transition(self, status, *, reason, now=None):
        if status not in STOPPED | {'active'} or not reason.strip():
            raise ValueError('valid status and reason required')
        now = time.time() if now is None else now
        with self.edit() as s:
            if status == 'active' and now >= s['deadline']:
                raise ValueError('deadline passed; explicit authorized revision required')
            s.update(status=status, reason=reason, pending=None, updated_at=now)

    def schedule(self, *, at, prompt, now=None, replace=False):
        now = time.time() if now is None else now
        with self.edit() as s:
            if s['status'] in STOPPED:
                raise ValueError('goal stopped; resume only when authorized')
            if not math.isfinite(at) or at < now + s['min_interval'] or at >= s['deadline']:
                raise ValueError('wake must respect minimum interval and precede deadline')
            if not prompt.strip():
                raise ValueError('nonempty wake prompt required')
            if s['pending'] and not replace:
                raise ValueError('pending wake exists; keep it or explicitly --replace')
            p = dict(token=uuid.uuid4().hex, due_at=at, next_attempt=at,
                     busy_deadline=min(at + 3600, s['deadline']), prompt=prompt, phase='scheduled')
            s.update(pending=p, status='waiting', updated_at=now)
            return p


class RPC:
    def __init__(self, path):
        import websocket  # websocket-client; never install automatically
        st = os.stat(path)
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
            raise ValueError('not a current-user Unix socket')
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(15)
        try:
            s.connect(path)
            self.ws = websocket.create_connection('ws://localhost', socket=s,
                                                   timeout=15, suppress_origin=True)
        except Exception:
            s.close(); raise
        self.seq = 0
        self.events = []
        try:
            self.call('initialize', {'clientInfo': {'name': 'scheduled_autonomy', 'version': '1.0'},
                                     'capabilities': {'experimentalApi': True}})
            self.ws.send(json.dumps({'method': 'initialized'}))
        except Exception:
            self.close(); raise

    def call(self, method, params):
        self.seq += 1
        self.ws.send(json.dumps({'id': self.seq, 'method': method, 'params': params}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.ws.settimeout(max(.1, min(15, deadline - time.monotonic())))
            m = json.loads(self.ws.recv())
            if m.get('id') == self.seq:
                if 'error' in m:
                    raise RuntimeError(m['error'])
                return m['result']
            self.events.append(m)
        raise TimeoutError(method)

    def read(self, thread):
        return self.call('thread/read', {'threadId': thread, 'includeTurns': False})['thread']

    def start(self, thread, prompt):
        # No thread creation, overrides, steering, or interruption.
        return self.call('turn/start', {'threadId': thread,
                                      'input': [{'type': 'text', 'text': prompt}]})['turn']

    def resume(self, thread):
        # Existing ID only; omit model/config/dynamicTools to preserve saved settings.
        return self.call('thread/resume', {'threadId': thread})['thread']

    def completion(self, thread, turn, timeout):
        import websocket
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.events:
                event = self.events.pop(0)
            else:
                self.ws.settimeout(max(.1, min(30, end - time.monotonic())))
                try:
                    event = json.loads(self.ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
            p = event.get('params', {})
            if (event.get('method') == 'turn/completed' and p.get('threadId') == thread
                    and p.get('turn', {}).get('id') == turn):
                return p['turn']
        return {'status': 'completion_unconfirmed'}

    def close(self):
        self.ws.close()


def halt(s, reason, now, status='waiting_user'):
    s.update(status=status, reason=reason, pending=None, updated_at=now)
    return reason


def alert(store, reason):
    """Durable visible status + best-effort desktop notification; never claim receipt."""
    record = {'at': time.time(), 'reason': reason, 'notification': 'unavailable'}
    try:
        result = subprocess.run(['notify-send', '--urgency=critical', 'Codex scheduled-autonomy',
                                 f'{store.thread}: {reason}; details: {store.path}'],
                                capture_output=True, text=True, timeout=5)
        record['notification'] = 'submitted' if result.returncode == 0 else 'unavailable'
        if result.returncode:
            record['notification_error'] = result.stderr[:500]
    except (OSError, subprocess.TimeoutExpired) as e:
        record['notification_error'] = str(e)
    with store.edit() as s:
        s['alert'] = record
    atomic_json(store.folder / 'ALERT.json', record)
    print(json.dumps({'event': 'ALERT', **record}, ensure_ascii=False), flush=True)


def record_completion(store, turn_id, result, now=None):
    now = time.time() if now is None else now
    with store.edit() as s:
        d = s.get('last_delivery') or {}
        if d.get('turn_id') != turn_id:
            return
        d.update(status=result['status'], observed_at=now, error=result.get('error'))
        # Do not overwrite a new wake or a deliberate completion/cancellation.
        if not s['pending'] and s['status'] not in STOPPED:
            halt(s, 'turn_finished_without_next_wake' if result['status'] == 'completed'
                 else 'turn_' + result['status'], now)


def retry(s, p, reason, now):
    p['last_error'] = reason
    p['next_attempt'] = min(now + 600, p['busy_deadline'])
    s['reason'] = reason
    return 'retry'


def tick(store, peer, now):
    """One non-model check; locks serialize cancellation/replacement with send."""
    started = time.monotonic()
    base_now = now
    with store.edit() as s:
        if s['status'] in STOPPED:
            return 'stopped'
        if now >= s['deadline']:
            return halt(s, 'expired', now, 'expired')
        p = s['pending']
        if not p:
            return 'no_pending'
        if p['phase'] == 'sending':
            return halt(s, 'unknown', now)
        if now < p['next_attempt']:
            return 'not_due'
        if now >= p['busy_deadline']:
            return halt(s, 'busy_timeout', now)
        t = peer.read(store.thread)
        s['last_observation'] = {'at': now, 'before': t['status']}
        now += time.monotonic() - started
        if now >= s['deadline']:
            return halt(s, 'expired', now, 'expired')
        if t['id'] != store.thread or str(Path(t['cwd']).resolve()) != s['cwd'] or s['host'] != socket.gethostname():
            return halt(s, 'identity_error', now)
        state = t['status']['type']
        if state == 'active':
            p['next_attempt'] = min(now + 600, p['busy_deadline'])
            return 'busy'
        if state not in ('idle', 'notLoaded'):
            return retry(s, p, 'thread_state:' + state, now)
        try:
            t = peer.resume(store.thread)  # also subscribes this connection to completion events
        except Exception as e:
            return retry(s, p, 'resume_error:' + str(e), now)
        s['last_observation']['after_resume'] = t['status']
        now = base_now + time.monotonic() - started
        if now >= s['deadline']:
            return halt(s, 'expired', now, 'expired')
        if t['id'] != store.thread or str(Path(t['cwd']).resolve()) != s['cwd']:
            return halt(s, 'identity_error', now)
        if t['status']['type'] != 'idle':
            return retry(s, p, 'after_resume:' + t['status']['type'], now)
        # Durable claim BEFORE network send: a crash/ambiguous response never auto-replays.
        p['phase'] = 'sending'
        atomic_json(store.path, s)
        prompt = (f"[scheduled-autonomy wake {p['token']}] 用户授权的后台触发。"
                  f"先读取 {Path(__file__).resolve().parents[1] / 'SKILL.md'} 和 {store.path}。"
                  "这是历史授权下的调度，不是新的权限；若目标停止或已到期，不继续工作。"
                  "仅在已有授权内执行下列检查，正常未变时不要重复汇报。"
                  "本轮结束前按Skill判断是否需要下一次唤醒，然后结束推理；不得启动原生active goal。\n"
                  + p['prompt'])
        try:
            turn = peer.start(store.thread, prompt)
        except Exception as e:
            s['last_delivery'] = {'at': now, 'token': p['token'], 'status': 'unknown', 'error': str(e)}
            return halt(s, 'unknown', now)
        s.update(last_delivery={'at': now, 'token': p['token'], 'status': 'accepted',
                                'turn_id': turn['id']}, pending=None, status='active', updated_at=now)
        return 'accepted'


def unit_name(thread):
    return 'codex-scheduled-autonomy-' + thread


def ensure_worker(store):
    unit = unit_name(store.thread)
    # A woken turn schedules its successor BEFORE its final answer. Do not kill
    # the connection observing that turn's completion in this normal path.
    if ((store.read().get('last_delivery') or {}).get('status') == 'accepted'
            and subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit]).returncode == 0):
        return
    # Restart only our timer, never the Codex server. Avoid an old worker exiting
    # just after is-active while a newly scheduled wake loses its consumer.
    subprocess.run(['systemctl', '--user', 'stop', unit], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    subprocess.run(['systemctl', '--user', 'reset-failed', unit], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    subprocess.run(['systemd-run', '--user', '--collect', '--unit=' + unit,
                    '--setenv=CODEX_THREAD_ID=' + store.thread,
                    sys.executable, str(Path(__file__).resolve()), '--root', str(store.folder.parent),
                    '--thread', store.thread, 'worker'], check=True)


def worker(store):
    # Dedicated worker lock prevents duplicate daemon instances, including manual runs.
    with (store.folder / 'worker.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        while True:
            s = store.read(); now = time.time()
            if s['status'] in STOPPED:
                return
            if now >= s['deadline']:
                with store.edit() as current:
                    # Re-read under lock: a concurrent authorized extension wins.
                    if time.time() >= current['deadline']:
                        halt(current, 'expired', time.time(), 'expired')
                continue
            p = s['pending']
            if not p or now < p['next_attempt']:
                time.sleep(min(60, max(1, (p['next_attempt'] if p else s['deadline']) - now)))
                continue
            try:
                peer = RPC(s['socket'])
                try:
                    outcome = tick(store, peer, time.time())
                    if outcome == 'accepted':
                        delivery = store.read()['last_delivery']
                        try:
                            result = peer.completion(store.thread, delivery['turn_id'],
                                                     max(1, min(1800, s['deadline'] - time.time())))
                        except Exception as e:
                            result = {'status': 'completion_unconfirmed', 'error': str(e)}
                        record_completion(store, delivery['turn_id'], result)
                        if result['status'] != 'completed' and store.read()['status'] != 'waiting_user':
                            alert(store, 'turn_' + result['status'])
                        print(json.dumps({'event': 'turn_result', 'turn_id': delivery['turn_id'],
                                          'status': result['status']}), flush=True)
                finally:
                    peer.close()
                print(json.dumps({'at': time.time(), 'event': outcome}), flush=True)
                current = store.read()
                if current['status'] == 'waiting_user':
                    alert(store, current.get('reason', outcome))
                    raise SystemExit(1)
                if outcome == 'retry' and not (current.get('alert') or {}).get('reason') == current.get('reason'):
                    alert(store, current['reason'])
            except Exception as e:
                with store.edit() as current:
                    if (current.get('pending') or {}).get('token') == p['token']:
                        if time.time() >= p['busy_deadline']:
                            halt(current, 'transport_error: ' + str(e), time.time())
                        else:
                            retry(current, current['pending'], 'transport_error: ' + str(e), time.time())
                print(json.dumps({'event': 'transport_error', 'error': str(e)}), flush=True)
                current = store.read()
                if current['status'] in STOPPED:
                    if current['status'] == 'waiting_user':
                        alert(store, current.get('reason', str(e)))
                        raise SystemExit(1)
                    return
                if not (current.get('alert') or {}).get('reason') == current.get('reason'):
                    alert(store, current.get('reason', str(e)))
                time.sleep(1)


def timestamp(value):
    from datetime import datetime
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError('include timezone, e.g. +08:00')
    return dt.timestamp()


def cli():
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(home / 'scheduled-autonomy'))
    p.add_argument('--thread', default=os.environ.get('CODEX_THREAD_ID'), help='defaults to current CODEX_THREAD_ID; never --last')
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('init')
    for key in ('objective', 'criteria', 'authorization'):
        a.add_argument('--' + key, required=True)
    a.add_argument('--deadline', type=timestamp, required=True)
    a.add_argument('--min-interval', type=int, default=3600, help='seconds; use project floor and estimated total/10')
    a.add_argument('--socket', default=str(home / 'app-server-control/app-server-control.sock'))
    a.add_argument('--native-goal-inactive', action='store_true', required=True, help='acknowledge native goal checked inactive')
    sub.add_parser('status'); sub.add_parser('probe'); sub.add_parser('worker')
    a = sub.add_parser('progress'); a.add_argument('--summary', required=True); a.add_argument('--next-action', required=True)
    a = sub.add_parser('schedule'); a.add_argument('--at', type=timestamp, required=True)
    a.add_argument('--prompt', required=True); a.add_argument('--replace', action='store_true')
    for cmd in ('pause', 'resume', 'cancel', 'complete', 'wait-user'):
        a = sub.add_parser(cmd); a.add_argument('--reason', required=True)
    a = sub.add_parser('revise'); a.add_argument('--authorization-note', required=True)
    a.add_argument('--deadline', type=timestamp); a.add_argument('--objective'); a.add_argument('--criteria')
    a.add_argument('--authorization'); a.add_argument('--min-interval', type=int)
    args = p.parse_args()
    if not args.thread:
        p.error('CODEX_THREAD_ID absent; verify explicit --thread against current host before proceeding')
    if os.environ.get('CODEX_THREAD_ID') and args.thread != os.environ['CODEX_THREAD_ID']:
        p.error('explicit thread conflicts with current CODEX_THREAD_ID')
    store = Store(args.root, args.thread)
    if args.cmd == 'init':
        peer = RPC(args.socket)
        try:
            t = peer.read(store.thread)
            if t['id'] != store.thread or str(Path(t['cwd']).resolve()) != str(Path.cwd().resolve()):
                raise ValueError('thread/project identity mismatch')
        finally:
            peer.close()
        store.initialize(objective=args.objective, criteria=args.criteria, authorization=args.authorization,
                         cwd=Path.cwd(), socket_path=args.socket, deadline=args.deadline, min_interval=args.min_interval)
    elif args.cmd == 'probe':
        s = store.read(); peer = RPC(s['socket'])
        try:
            t = peer.read(store.thread)
            print(json.dumps({'id': t['id'], 'cwd': t['cwd'], 'status': t['status']}))
        finally:
            peer.close()
        return
    elif args.cmd == 'worker':
        worker(store); return
    elif args.cmd == 'schedule':
        with (store.folder / 'launch.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pending = store.schedule(at=args.at, prompt=args.prompt, replace=args.replace)
            try:
                ensure_worker(store)
            except Exception:
                with store.edit() as current:
                    if (current.get('pending') or {}).get('token') == pending['token']:
                        halt(current, 'worker launch failed; no scheduled wake guaranteed', time.time())
                raise
    elif args.cmd == 'progress':
        store.progress(args.summary, args.next_action)
    elif args.cmd == 'revise':
        changes = {k: getattr(args, k) for k in ('objective', 'criteria', 'authorization', 'deadline', 'min_interval') if getattr(args, k) is not None}
        store.revise(changes, authorization_note=args.authorization_note)
    elif args.cmd != 'status':
        state = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled',
                 'complete': 'completed', 'wait-user': 'waiting_user'}[args.cmd]
        store.transition(state, reason=args.reason)
    s = store.read()
    s['worker_unit'] = unit_name(store.thread)
    print(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        cli()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
