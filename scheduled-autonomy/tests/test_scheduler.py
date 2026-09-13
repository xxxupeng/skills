import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import threading
import subprocess
import os
import sys

SPEC = importlib.util.spec_from_file_location('scheduler', Path(__file__).parents[1] / 'scripts' / 'scheduler.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

class Peer:
    def __init__(self, status='idle', failure=False):
        self.status, self.failure, self.sent = status, failure, []
    def read(self, thread):
        return {'id': thread, 'cwd': '/tmp', 'status': {'type': self.status}}
    def start(self, thread, prompt):
        self.sent.append((thread, prompt))
        if self.failure:
            raise TimeoutError('reply lost')
        return {'id': 'turn-test', 'status': 'inProgress'}
    def resume(self, thread):
        if self.status == 'notLoaded': self.status = 'idle'
        return self.read(thread)

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = m.Store(Path(self.tmp.name), '11111111-1111-4111-8111-111111111111')
        self.store.initialize(objective='test', criteria='fixed quality', authorization='read only',
                              cwd='/tmp', socket_path='/tmp/test.sock', deadline=10000,
                              min_interval=60, now=100)
    def tearDown(self):
        self.tmp.cleanup()
    def arm(self):
        return self.store.schedule(at=200, prompt='check only', now=100)
    def test_schedule_rejects_fast_poll_and_after_deadline(self):
        for at in (120, 10001):
            with self.assertRaises(ValueError):
                self.store.schedule(at=at, prompt='check', now=100)
    def test_no_silent_replacement(self):
        self.arm()
        with self.assertRaises(ValueError):
            self.store.schedule(at=300, prompt='other', now=100)
        self.assertEqual(self.store.read()['pending']['prompt'], 'check only')
    def test_deliver_exactly_once_including_second_worker(self):
        self.arm(); peer = Peer()
        self.assertEqual(m.tick(self.store, peer, 200), 'accepted')
        m.tick(self.store, peer, 201)
        self.assertEqual(len(peer.sent), 1)
        self.assertIsNone(self.store.read()['pending'])
        self.assertEqual(self.store.read()['last_delivery']['turn_id'], 'turn-test')
    def test_busy_defers_without_model_then_delivers(self):
        self.arm(); peer = Peer('active')
        self.assertEqual(m.tick(self.store, peer, 200), 'busy')
        self.assertEqual(peer.sent, [])
        peer.status = 'idle'
        self.assertEqual(m.tick(self.store, peer, 801), 'accepted')
    def test_busy_timeout_waits_user(self):
        self.arm(); peer = Peer('active')
        m.tick(self.store, peer, 200)
        self.assertEqual(m.tick(self.store, peer, 3801), 'busy_timeout')
        self.assertEqual(self.store.read()['status'], 'waiting_user')
        self.assertEqual(peer.sent, [])
    def test_expiry_never_sends(self):
        self.arm(); peer = Peer()
        self.assertEqual(m.tick(self.store, peer, 10000), 'expired')
        self.assertEqual(peer.sent, [])
    def test_cancel_and_pause_clear_wakeup(self):
        for state in ('paused', 'cancelled', 'completed', 'waiting_user'):
            self.store.transition('active', reason='resume', now=100)
            self.arm()
            self.store.transition(state, reason='user request', now=150)
            peer = Peer(); m.tick(self.store, peer, 200)
            self.assertEqual(peer.sent, [])
            self.assertIsNone(self.store.read()['pending'])
    def test_unknown_send_not_retried(self):
        self.arm(); peer = Peer(failure=True)
        self.assertEqual(m.tick(self.store, peer, 200), 'unknown')
        m.tick(self.store, peer, 900)
        self.assertEqual(len(peer.sent), 1)
        self.assertEqual(self.store.read()['status'], 'waiting_user')
    def test_crashed_sending_is_not_replayed(self):
        self.arm()
        with self.store.edit() as s:
            s['pending']['phase'] = 'sending'
        peer = Peer()
        self.assertEqual(m.tick(self.store, peer, 200), 'unknown')
        self.assertEqual(peer.sent, [])
    def test_wrong_identity_refuses_send(self):
        self.arm(); peer = Peer()
        peer.read = lambda t: {'id': t, 'cwd': '/other', 'status': {'type': 'idle'}}
        self.assertEqual(m.tick(self.store, peer, 200), 'identity_error')
        self.assertEqual(peer.sent, [])
    def test_progress_update_preserves_budget(self):
        self.store.progress('done A', 'evaluate B', now=150)
        s = self.store.read()
        self.assertEqual(s['deadline'], 10000)
        self.assertEqual(s['created_at'], 100)
    def test_goal_revision_requires_explicit_record(self):
        with self.assertRaises(ValueError):
            self.store.revise({'deadline': 20000}, authorization_note='', now=150)
        self.store.revise({'deadline': 20000}, authorization_note='user extends at 150', now=150)
        s = self.store.read()
        self.assertEqual(s['deadline'], 20000)
        self.assertEqual(s['revisions'][-1]['before']['deadline'], 10000)
    def test_not_loaded_resumes_original_and_delivers(self):
        self.arm(); peer = Peer('notLoaded')
        self.assertEqual(m.tick(self.store, peer, 200), 'accepted')
        self.assertEqual(peer.sent[0][0], self.store.thread)
        self.assertEqual(self.store.read()['last_observation']['before']['type'], 'notLoaded')
    def test_resume_changed_identity_never_sends(self):
        self.arm(); peer = Peer('notLoaded')
        peer.resume = lambda t: {'id': 'wrong', 'cwd': '/tmp', 'status': {'type': 'idle'}}
        self.assertEqual(m.tick(self.store, peer, 200), 'identity_error')
        self.assertEqual(peer.sent, [])
    def test_system_error_keeps_raw_state_and_defers(self):
        self.arm(); peer = Peer('systemError')
        self.assertEqual(m.tick(self.store, peer, 200), 'retry')
        self.assertEqual(self.store.read()['last_observation']['before']['type'], 'systemError')
        self.assertIsNotNone(self.store.read()['pending'])
        self.assertEqual(peer.sent, [])
    def test_resume_crossing_deadline_never_sends(self):
        self.store.schedule(at=9800, prompt='check', now=100); peer = Peer('notLoaded')
        with patch.object(m.time, 'monotonic', side_effect=[0, 1, 20]):
            self.assertEqual(m.tick(self.store, peer, 9990), 'expired')
        self.assertEqual(peer.sent, [])
    def test_delivery_completion_does_not_clear_new_schedule(self):
        self.arm(); m.tick(self.store, Peer(), 200)
        self.store.schedule(at=500, prompt='next', now=201)
        m.record_completion(self.store, 'turn-test', {'status':'completed'}, now=250)
        self.assertEqual(self.store.read()['pending']['prompt'], 'next')
        self.assertEqual(self.store.read()['last_delivery']['status'], 'completed')
    def test_alert_is_persisted_even_without_desktop(self):
        with patch.object(m.subprocess, 'run', side_effect=FileNotFoundError('notify-send')):
            m.alert(self.store, 'test failure')
        self.assertEqual(self.store.read()['alert']['notification'], 'unavailable')
        self.assertTrue((self.store.folder / 'ALERT.json').exists())
    def test_failed_edit_leaves_state_intact(self):
        before = self.store.read()
        with self.assertRaises(RuntimeError):
            with self.store.edit() as s:
                s['objective'] = 'bad'
                raise RuntimeError('abort')
        self.assertEqual(self.store.read(), before)
    def test_slow_read_crossing_deadline_never_sends(self):
        self.store.schedule(at=9800, prompt='check', now=100)
        peer = Peer()
        with patch.object(m.time, 'monotonic', side_effect=[0, 20]):
            self.assertEqual(m.tick(self.store, peer, 9990), 'expired')
        self.assertEqual(peer.sent, [])
    def test_two_parallel_senders_one_attempt(self):
        self.arm(); peer = Peer(); results = []
        threads = [threading.Thread(target=lambda: results.append(m.tick(self.store, peer, 200))) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=2)
        self.assertEqual(sorted(results), ['accepted', 'no_pending'])
        self.assertEqual(len(peer.sent), 1)
    def test_explicit_thread_cannot_override_current_conversation(self):
        env = dict(os.environ, CODEX_THREAD_ID=self.store.thread)
        result = subprocess.run([sys.executable, str(Path(m.__file__)), '--root', self.tmp.name,
                                 '--thread', '22222222-2222-4222-8222-222222222222', 'status'],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('conflicts', result.stderr)
        self.assertFalse((Path(self.tmp.name) / '22222222-2222-4222-8222-222222222222').exists())
    def test_repeat_init_cannot_reset_budget(self):
        with self.assertRaises(ValueError):
            self.store.initialize(objective='new', criteria='new', authorization='read only',
                                  cwd='/tmp', socket_path='/tmp/test.sock', deadline=90000, now=500)
        self.assertEqual(self.store.read()['deadline'], 10000)
    def test_worker_transport_failure_does_not_overwrite_concurrent_cancel(self):
        self.arm()
        def failing_rpc(path):
            self.store.transition('cancelled', reason='user cancels')
            raise OSError('disconnected')
        with patch.object(m.time, 'time', return_value=200), patch.object(m, 'RPC', side_effect=failing_rpc):
            m.worker(self.store)
        self.assertEqual(self.store.read()['status'], 'cancelled')
    def test_accepted_then_disconnect_is_unconfirmed_not_silent_active(self):
        self.arm(); peer = Peer()
        peer.completion = lambda *args: (_ for _ in ()).throw(OSError('lost socket'))
        peer.close = lambda: None
        with patch.object(m.time, 'time', return_value=200), patch.object(m, 'RPC', return_value=peer), patch.object(m, 'alert'):
            with self.assertRaises(SystemExit): m.worker(self.store)
        self.assertEqual(self.store.read()['last_delivery']['status'], 'completion_unconfirmed')
        self.assertEqual(self.store.read()['status'], 'waiting_user')
        self.assertEqual(len(peer.sent), 1)
    def test_scheduling_successor_preserves_current_observer(self):
        self.arm(); m.tick(self.store, Peer(), 200)
        self.store.schedule(at=500, prompt='next', now=201)
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(m.subprocess, 'run', side_effect=command):
            m.ensure_worker(self.store)
        self.assertEqual(calls, [['systemctl', '--user', 'is-active', '--quiet', m.unit_name(self.store.thread)]])

if __name__ == '__main__':
    unittest.main()
