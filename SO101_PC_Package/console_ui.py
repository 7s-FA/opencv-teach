"""Bounded live console: latest arm states and the last two entered commands."""
from collections import deque
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window, ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from console_text import arm_name, state_name


class CommandConsole:
    def __init__(self, *, log_path=None):
        self.app=None;self.worker=None;self.lock=threading.RLock();self.context=threading.local()
        self.commands=deque(maxlen=2);self.communication={};self.activity={};self.notice=''
        self.help_text='';self.incoming=queue.Queue();self.closed=False
        self.log_path=log_path;self.audit_logger=None

    def audit(self, message):
        try:
            if self.audit_logger is None:
                path=self.log_path or Path(os.environ.get('SO101_CLIENT_STATE_DIR',str(Path.home()/'.local/state/so101-supervision-client')))/'console.log'
                path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
                logger=logging.Logger('operator-console');handler=RotatingFileHandler(path,maxBytes=2_000_000,backupCount=2,encoding='utf-8')
                handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'));logger.addHandler(handler)
                self.audit_logger=logger
            self.audit_logger.info(message)
        except OSError:
            self.notice='표시 로그 저장 실패 · 화면의 결과와 업무 기록을 확인하세요.'

    def changed(self):
        if self.app is not None:self.app.invalidate()

    def begin_command(self, command):
        row={'command':command.upper(),'at':datetime.now().strftime('%H:%M:%S'),'status':'입력 접수','request_id':None}
        with self.lock:self.commands.append(row);self.context.current=row;self.audit('명령 입력: '+row['command'])
        self.changed()

    def end_command(self):
        row=getattr(self.context,'current',None)
        if row is not None and row['status']=='입력 접수':row['status']='확인 완료'
        self.context.current=None;self.changed()

    def connection(self, arm, state):
        with self.lock:
            self.communication[arm]=(state_name(state),datetime.now().strftime('%H:%M:%S'))
            self.audit(arm_name(arm)+' 관제 통신: '+state_name(state))
        if not (sys.stdin.isatty() and sys.stdout.isatty()):print(arm_name(arm)+' 관제 통신: '+state_name(state),flush=True)
        self.changed()

    def request(self, arm, command, key, status, detail=''):
        text=arm_name(arm)+' · '+status+(' · '+detail if detail else '')
        with self.lock:
            current=getattr(self.context,'current',None)
            row=current or next((r for r in reversed(self.commands) if r['request_id']==key),None)
            if row is not None:row.update(request_id=key,status=text)
            self.activity[arm]=command+' · '+status
            self.audit(f'{arm} {command} · {status} · {detail} · 업무 {key}')
        if not (sys.stdin.isatty() and sys.stdout.isatty()):print(command+' · '+text,flush=True)
        self.changed()

    def write(self, message, level='알림'):
        with self.lock:
            row=getattr(self.context,'current',None)
            if row is not None:row['status']=message
            else:self.notice=level+': '+message
            self.audit(level+': '+message)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):print(message,flush=True)
        self.changed()

    def show_help(self, text):
        self.help_text=text.strip()
        if not (sys.stdin.isatty() and sys.stdout.isatty()):print(self.help_text,flush=True)
        if self.app is not None:
            def show():
                self.help_area.text=self.help_text
                self.app.layout.focus(self.help_area)
                self.app.invalidate()
            if self.app.loop is not None:self.app.loop.call_soon_threadsafe(show)

    def status_text(self):
        with self.lock:
            lines=[]
            for arm in ('arm2','arm3'):
                status,stamp=self.communication.get(arm,('연결 확인 중','--:--:--'))
                lines.append(f'{arm_name(arm)}  {status}  [{stamp}]  |  '+self.activity.get(arm,'이 관제의 업무 기록 없음'))
            return '\n'.join(lines)

    def history_text(self):
        with self.lock:
            rows=['최근 명령 (최대 2개 · 최신순)']
            if not self.commands:rows.append('아직 입력한 명령이 없습니다.')
            for row in reversed(self.commands):rows.extend([f"{row['at']}  {row['command']}",'  '+row['status']])
            return '\n'.join(rows)

    def create_application(self, *, input=None, output=None):
        def submit(buffer):
            value=buffer.text.strip()
            if value:self.incoming.put(value)
            return False
        self.input_area=TextArea(height=1,prompt='명령> ',multiline=False,accept_handler=submit)
        self.help_area=TextArea(text=self.help_text,read_only=True,scrollbar=True,wrap_lines=True,
                                focus_on_click=True,height=Dimension(min=3,preferred=20))
        self.history_area=TextArea(text=self.history_text(),read_only=True,scrollbar=True,wrap_lines=True,
                                   focus_on_click=True,height=Dimension(min=3,preferred=5,max=9))
        bindings=KeyBindings()
        @bindings.add('c-c')
        @bindings.add('c-d')
        def quit_app(event):
            self.closed=True;self.incoming.put(None);event.app.exit()
        @bindings.add('escape')
        def focus_input(event):
            event.app.layout.focus(self.input_area)
        @bindings.add('f2')
        def focus_history(event):
            event.app.layout.focus(self.history_area)
        @bindings.add('f1')
        def focus_help(event):
            event.app.layout.focus(self.input_area if event.app.layout.has_focus(self.help_area) else self.help_area)
        layout=HSplit([
            self.help_area,
            Window(FormattedTextControl(lambda:'연결 상태\n'+self.status_text()),
                   height=Dimension(min=3,preferred=3,max=5),wrap_lines=True),
            self.history_area,
            ConditionalContainer(Window(FormattedTextControl(lambda:self.notice),
                height=Dimension(min=1,preferred=2,max=3),wrap_lines=True,style='class:notice'),
                filter=Condition(lambda:bool(self.notice))),
            Window(FormattedTextControl('F1 명령 목록 · F2 최근 명령 · 방향키/PgUp/PgDn · Esc 입력'),height=1),
            self.input_area,
        ])
        def refresh_history(app):
            text=self.history_text()
            if self.history_area.text!=text:
                cursor=self.history_area.buffer.cursor_position if app.layout.has_focus(self.history_area) else 0
                self.history_area.text=text
                self.history_area.buffer.cursor_position=min(cursor,len(text))
        self.app=Application(layout=Layout(layout,focused_element=self.input_area),key_bindings=bindings,
            full_screen=True,mouse_support=True,style=Style.from_dict({'title':'bold','notice':'ansiyellow'}),
            input=input,output=output,min_redraw_interval=.05,before_render=refresh_history)
        return self.app

    def read(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):return input('명령> ')
        if self.closed:raise EOFError()
        if self.worker is None:
            app=self.create_application()
            def run():
                try:app.run()
                finally:self.closed=True;self.incoming.put(None)
            self.worker=threading.Thread(target=run,name='operator-console',daemon=True);self.worker.start()
        value=self.incoming.get()
        if value is None:raise EOFError()
        return value

    def close(self):
        self.closed=True;self.incoming.put(None)
        app=self.app
        if app is not None and app.is_running and app.loop is not None:
            def finish():
                if app.is_running and not app.is_done:app.exit()
            app.loop.call_soon_threadsafe(finish)
        if self.worker is not None and self.worker is not threading.current_thread():self.worker.join(2)
        if self.audit_logger:
            for handler in self.audit_logger.handlers:handler.close()


console=CommandConsole()


class PromptLogger:
    def __init__(self, logger):self.logger=logger
    def info(self, message):console.write(message)
    def warning(self, message):console.write(message,'주의')
    def error(self, message):console.write(message,'오류')
    def connection(self, arm, state):console.connection(arm,state)
    def request(self, arm, command, key, status, detail=''):console.request(arm,command,key,status,detail)
