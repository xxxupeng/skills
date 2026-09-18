"""Dual transport contracts; local process fixtures, never invoke a model."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from test_scheduler import m, Peer

THREAD = '11111111-1111-4111-8111-111111111111'


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.rollout = self.home / 'sessions/2026/09/18' / ('rollout-' + THREAD + '.jsonl')
        self.rollout.parent.mkdir(parents=True)
        self.meta = dict(id=THREAD, cwd=str(self.home), source='cli', model_provider='openai')
        self.context = dict(cwd=str(self.home), model='test-model', effort='medium',
                            approval_policy='never', sandbox_policy={'type': 'read-only'})
        self.write_session()
        self.state = dict(thread_id=THREAD, cwd=str(self.home), host=m.socket.gethostname(),
                          socket=str(self.home/'missing.sock'), backend='auto',
                          codex_home=str(self.home), codex_binary=sys.executable)

    def tearDown(self):
        self.tmp.cleanup()

    def write_session(self, active=False):
        rows = [dict(type='session_meta', payload=self.meta),
                dict(type='turn_context', payload=self.context),
                dict(type='event_msg', payload={'type':'task_started', 'turn_id':'old'})]
        if not active:
            rows.append(dict(type='event_msg', payload={'type':'task_complete','turn_id':'old'}))
        self.rollout.write_text(''.join(json.dumps(row)+'\n' for row in rows))

    def factory(self):
        self.assertTrue(hasattr(m, 'open_peer'), 'missing dual-backend selection')
        return m.open_peer(self.state, self.home / 'delivery')

    def test_auto_prefers_available_server(self):
        with patch.object(m, 'RPC', return_value=Peer()) as rpc:
            peer = self.factory()
        self.assertIsInstance(peer, Peer)

    def test_auto_missing_socket_selects_local_cli(self):
        peer = self.factory()
        try:
            self.assertEqual(peer.backend, 'cli')
            self.assertEqual(peer.read(THREAD)['status']['type'], 'idle')
        finally:
            peer.close()

    def test_cli_does_not_require_websocket_dependency_when_socket_absent(self):
        import builtins
        real_import = builtins.__import__
        def without_websocket(name, *args, **kwargs):
            if name == 'websocket':
                raise ModuleNotFoundError('websocket is not installed')
            return real_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=without_websocket):
            peer = self.factory()
        peer.close()

    def test_explicit_cli_does_not_contact_server(self):
        self.state['backend'] = 'cli'
        with patch.object(m, 'RPC', side_effect=AssertionError('unexpected server access')):
            peer = self.factory()
        peer.close()

    def test_known_app_goal_cannot_gain_cli_fallback_without_binding(self):
        self.state['selected_at_init'] = 'app-server'
        with self.assertRaisesRegex(ValueError, 'bind|fallback'):
            self.factory()

    def test_legacy_goals_never_silently_switch(self):
        self.state.pop('backend')
        with self.assertRaises(FileNotFoundError):
            self.factory()

    def test_timeout_does_not_fallback(self):
        with patch.object(m, 'RPC', side_effect=TimeoutError('uncertain')):
            with self.assertRaises(TimeoutError):
                self.factory()

    def test_cli_refuses_desktop_dynamic_tools(self):
        self.meta.update(source='vscode', dynamic_tools=[{'name':'app_tool'}])
        self.write_session()
        with self.assertRaisesRegex(ValueError, 'CLI|dynamic|desktop'):
            self.factory()

    def test_wrong_cwd_refused_before_launch(self):
        self.meta['cwd'] = '/other'; self.write_session()
        with self.assertRaisesRegex(ValueError, 'identity|cwd'):
            self.factory()

    def test_cli_unfinished_turn_is_busy(self):
        self.write_session(active=True)
        peer = self.factory()
        try:
            self.assertEqual(peer.read(THREAD)['status']['type'], 'active')
        finally:
            peer.close()

    def test_cli_open_writer_is_busy_even_after_completed_turn(self):
        peer = self.factory()
        try:
            with self.rollout.open('a'):
                self.assertEqual(peer.read(THREAD)['status']['type'], 'active')
        finally:
            peer.close()

    def test_cli_argv_preserves_explicit_id_model_and_sandbox(self):
        peer = self.factory()
        try:
            cmd = peer.command(THREAD)
            self.assertEqual(cmd[-4:], ['resume', '--json', THREAD, '-'])
            self.assertIn('read-only', cmd)
            self.assertIn('test-model', cmd)
            self.assertNotIn('--last', cmd)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)
        finally:
            peer.close()

    def test_cli_process_jsonl_and_exit_required_for_completion(self):
        peer = self.factory()
        script = self.home / 'fixture.py'
        script.write_text('import sys,json\n'
                          'p=sys.stdin.read()\n'
                          'assert "wake test" in p\n'
                          'print(json.dumps({"type":"thread.started","thread_id":sys.argv[1]}))\n'
                          'print(json.dumps({"type":"turn.started"}))\n'
                          'print(json.dumps({"type":"turn.completed"}))\n')
        try:
            with patch.object(peer, 'command', return_value=[sys.executable,str(script),THREAD]):
                turn = peer.start(THREAD, 'wake test')
            self.assertEqual(peer.completion(THREAD,turn['id'],3)['status'], 'completed')
            self.assertTrue(list((self.home/'delivery').glob('*.jsonl')))
        finally:
            peer.close()

    def test_cli_zero_exit_without_completion_is_not_success(self):
        peer = self.factory()
        try:
            with patch.object(peer, 'command', return_value=[sys.executable,'-c','import sys; sys.stdin.read()']):
                turn = peer.start(THREAD, 'wake test')
            self.assertEqual(peer.completion(THREAD,turn['id'],3)['status'], 'completion_unconfirmed')
        finally:
            peer.close()

    def test_cli_wrong_emitted_id_never_counts_as_completed(self):
        peer = self.factory()
        code = 'import sys; sys.stdin.read(); print(\'{"type":"thread.started","thread_id":"wrong"}\'); print(\'{"type":"turn.completed"}\')'
        try:
            with patch.object(peer, 'command', return_value=[sys.executable, '-c', code]):
                turn = peer.start(THREAD, 'test')
            self.assertEqual(peer.completion(THREAD, turn['id'], 3)['status'], 'completion_unconfirmed')
        finally:
            peer.close()

    def test_cli_config_drift_refuses_resume(self):
        peer = self.factory()
        try:
            self.state['cli_identity'] = peer.identity
            (self.home/'config.toml').write_text('model="changed"\n')
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                self.factory()
        finally:
            peer.close()

    def test_cli_requires_noninteractive_permission_contract(self):
        self.context['approval_policy'] = 'on-request'; self.write_session()
        with self.assertRaisesRegex(ValueError, 'approval_policy'):
            self.factory()

    def test_cli_same_prompt_no_second_launch_after_ambiguous_receipt(self):
        store = m.Store(self.home/'goals', THREAD)
        store.initialize(objective='test',criteria='test',authorization='test',
                         cwd=self.home,socket_path=self.state['socket'],deadline=10000,
                         min_interval=60,now=100)
        store.schedule(at=200,prompt='test',now=100)
        peer = self.factory()
        count = []
        def lost_receipt(*args):
            count.append(1)
            raise OSError('uncertain subprocess start')
        try:
            with patch.object(peer, 'start', side_effect=lost_receipt):
                self.assertEqual(m.tick(store,peer,200),'unknown')
                m.tick(store,peer,900)
            self.assertEqual(len(count),1)
        finally:
            peer.close()

    def test_unconfirmed_completion_cancels_successor(self):
        store = m.Store(self.home/'goals', THREAD)
        store.initialize(objective='test',criteria='test',authorization='test',cwd='/tmp',
                         socket_path='/tmp/unused',deadline=10000,min_interval=60,now=100)
        store.schedule(at=200,prompt='test',now=100)
        m.tick(store, Peer(), 200)
        store.schedule(at=500,prompt='next',now=250)
        m.record_completion(store,'turn-test',{'status':'completion_unconfirmed'},now=300)
        self.assertEqual(store.read()['status'], 'waiting_user')
        self.assertIsNone(store.read()['pending'])

    def test_auto_cli_init_and_real_subprocess_delivery(self):
        import subprocess
        binary = self.home / 'fake-codex'
        binary.write_text('#!' + sys.executable + '\n'
                          'import sys,json,os\n'
                          'args=sys.argv[1:]\n'
                          'assert args[-4:-2] == ["resume","--json"]\n'
                          'assert "--last" not in args\n'
                          'prompt=sys.stdin.read()\n'
                          'assert "scheduled-autonomy wake" in prompt\n'
                          'assert "CODEX_THREAD_ID" not in os.environ\n'
                          'print(json.dumps({"type":"thread.started","thread_id":args[-2]}))\n'
                          'print(json.dumps({"type":"turn.started"}))\n'
                          'print(json.dumps({"type":"turn.completed"}))\n')
        binary.chmod(0o700)
        from datetime import datetime, timezone
        deadline = datetime.fromtimestamp(time.time()+3600, timezone.utc).isoformat()
        root = self.home / 'state'
        env = dict(os.environ, CODEX_HOME=str(self.home), CODEX_THREAD_ID=THREAD)
        result = subprocess.run([sys.executable, str(Path(m.__file__)), '--root',str(root),
                                 'init','--objective','test','--criteria','reply','--authorization','test only',
                                 '--deadline',deadline,'--min-interval','1','--native-goal-inactive',
                                 '--codex-binary',str(binary)],
                                cwd=self.home,env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        state = json.loads(result.stdout)
        self.assertEqual(state['backend'],'auto')
        self.assertEqual(state['selected_at_init'],'cli')
        self.assertEqual(state['cli_identity']['sandbox'],{'type':'read-only'})
        store = m.Store(root,THREAD)
        now = time.time()
        store.schedule(at=now+1,prompt='test only',now=now)
        peer = m.open_peer(store.read(),store.folder/'deliveries')
        try:
            self.assertEqual(m.tick(store,peer,now+1),'accepted')
            delivery = store.read()['last_delivery']
            self.assertEqual(delivery['backend'],'cli')
            completion = peer.completion(THREAD,delivery['turn_id'],3)
            m.record_completion(store,delivery['turn_id'],completion)
            self.assertEqual(store.read()['last_delivery']['status'],'completed')
            self.assertEqual(store.read()['status'],'waiting_user')
            self.assertIsNone(store.read()['pending'])
        finally:
            peer.close()


if __name__ == '__main__':
    unittest.main()
