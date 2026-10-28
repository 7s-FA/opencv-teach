"""Display-only updates must not grow the screen or alter command input."""
from pathlib import Path
import sys,tempfile,threading,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'SO101_PC_Package'))
from console_ui import CommandConsole
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput


class ConsoleDisplayTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.path=Path(temp.name)/'console.log';self.c=CommandConsole(log_path=self.path)
        self.addCleanup(self.c.close)

    def test_connection_replaces_one_row_per_arm_and_keeps_history_in_file(self):
        for state in ('COMM_LOST','ONLINE')*5:self.c.connection('arm2',state)
        self.c.connection('arm3','ONLINE')
        text=self.c.status_text()
        self.assertEqual(len(text.splitlines()),2);self.assertEqual(text.count('연결됨'),2)
        self.assertNotIn('통신 끊김',text);self.assertIn('통신 끊김',self.path.read_text())
        self.assertFalse(self.c.commands)

    def test_last_two_commands_survive_feedback_and_automatic_queries(self):
        for i in range(3):
            self.c.begin_command('BUILD_'+str(i))
            self.c.request('arm2','BUILD_'+str(i),str(i),'실행 요청');self.c.end_command()
        for _ in range(8):self.c.request('arm2','BUILD_2','2','기존 요청 상태 조회')
        self.c.request('arm2','BUILD_0','0','완료')
        shown=self.c.history_text()
        self.assertEqual(len(self.c.commands),2);self.assertNotIn('BUILD_0',shown)
        self.assertIn('BUILD_1',shown);self.assertIn('BUILD_2',shown)
        self.assertIn('BUILD_0',self.path.read_text())

    def test_manual_retry_is_a_new_command_but_result_updates_only_its_row(self):
        self.c.begin_command('BUILD_A');self.c.request('arm2','BUILD_A','key','실행 요청');self.c.end_command()
        self.c.begin_command('RETRY');self.c.request('arm2','BUILD_A','key','기존 요청 상태 조회');self.c.end_command()
        self.c.request('arm2','BUILD_A','key','완료')
        self.assertIn('실행 요청',self.c.commands[0]['status'])
        self.assertIn('완료',self.c.commands[1]['status'])

    def test_live_refresh_preserves_partially_typed_input_and_enter(self):
        with create_pipe_input() as pipe:
            app=self.c.create_application(input=pipe,output=DummyOutput())
            ready=threading.Event();done=threading.Event()
            app.after_render += lambda _:ready.set()
            worker=threading.Thread(target=lambda:app.run());worker.start()
            try:
                self.assertTrue(ready.wait(3));ready.clear()
                pipe.send_text('BUILD_')
                app.loop.call_soon_threadsafe(lambda:None)
                # Wait for rendering triggered by the input, then update from the ROS thread.
                self.assertTrue(ready.wait(3));ready.clear()
                self.c.connection('arm2','COMM_LOST');self.c.connection('arm2','ONLINE')
                self.assertTrue(ready.wait(3))
                pipe.send_text('A\r')
                self.assertEqual(self.c.incoming.get(timeout=3),'BUILD_A')
                pipe.send_text('\x03')
                self.assertIsNone(self.c.incoming.get(timeout=3))
            finally:
                self.c.close();worker.join(3)
            self.assertFalse(worker.is_alive())
