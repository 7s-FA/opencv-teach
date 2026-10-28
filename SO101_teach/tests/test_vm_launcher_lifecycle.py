"""Launcher interruption never propagates a hard power-off to the guest VM."""
import importlib.util
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch


class VmLauncherLifecycleTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / 'SO101_WpfConsole/scripts/open-console.py'
        spec = importlib.util.spec_from_file_location('launcher_lifecycle_fixture', path)
        self.module = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.module)
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)

    def test_interrupt_during_guest_wait_closes_bridge_but_never_kills_guest(self):
        bridge = Mock(); bridge.poll.return_value = None
        vm = Mock(); vm.wait.side_effect = KeyboardInterrupt
        m = self.module
        with patch.object(m, 'check'), patch.object(m, 'notify'), patch.object(m, 'STATE', Path(self.directory.name)), \
             patch.object(m, 'bridge_ready', side_effect=[False, True]), patch.object(m, 'vm_pid', return_value=None), \
             patch.object(m.subprocess, 'Popen', side_effect=[bridge, vm]) as start, patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(KeyboardInterrupt): m.main()
        self.assertEqual(start.call_count, 2)
        self.assertTrue(start.call_args_list[1].kwargs['start_new_session'])
        run.assert_not_called()
        vm.kill.assert_not_called(); vm.terminate.assert_not_called(); vm.send_signal.assert_not_called()
        bridge.send_signal.assert_called_once_with(signal.SIGINT)
        bridge.wait.assert_called_once_with(timeout=15)

    def test_existing_guest_is_not_relaunched_or_stopped_on_launcher_cancel(self):
        bridge = Mock(); bridge.poll.return_value = None
        m = self.module
        with patch.object(m, 'check'), patch.object(m, 'notify'), patch.object(m, 'STATE', Path(self.directory.name)), \
             patch.object(m, 'bridge_ready', side_effect=[False, True]), \
             patch.object(m, 'vm_pid', side_effect=[123, KeyboardInterrupt]), \
             patch.object(m.subprocess, 'Popen', return_value=bridge) as start, patch.object(m.subprocess, 'run') as run:
            with self.assertRaises(KeyboardInterrupt): m.main()
        start.assert_called_once(); run.assert_not_called()
        self.assertIn('start-bridge.sh', str(start.call_args.args[0]))
        bridge.send_signal.assert_called_once_with(signal.SIGINT)
