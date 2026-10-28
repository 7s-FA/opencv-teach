"""Maximized teaching controls and recipe isolation; no live connections."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from tests import test_gui_pi_execution as existing
from so101_teach import pc_chain_ui
from so101_teach.pc_chain_execution import PcChainExecution
from so101_teach.pc_chain_receiver import validate
from so101_teach.episode_inspection import criteria_signature

class PcChainUiTests(unittest.TestCase):
    setUp=existing.GuiExecutionTests.setUp
    tearDown=existing.GuiExecutionTests.tearDown
    ready=existing.GuiExecutionTests.ready
    def test_separate_controls_show_offline_gate_and_never_launch(self):
        self.root.geometry('1920x1043+0+0');self.root.update()
        with patch.object(pc_chain_ui,'configuration',return_value={'physical_execution_enabled':False}),patch('so101_teach.pc_chain_execution.PcChainExecution') as launch:
            window=pc_chain_ui.show(self.a);self.root.update()
            self.assertEqual(len(window.pc_buttons),2)
            self.assertTrue(window.close_button.winfo_ismapped())
            self.assertLessEqual(window.close_button.winfo_rooty()+window.close_button.winfo_height(),window.winfo_rooty()+window.winfo_height())
            for button in window.pc_buttons:
                self.assertTrue(button.instate(['disabled']));button.invoke()
            launch.assert_not_called()
            self.assertTrue(self.a.pc_chain_btn.winfo_ismapped())
            self.assertLessEqual(self.a.pc_chain_btn.winfo_rooty()+self.a.pc_chain_btn.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
            window.destroy()
    def test_chain_request_uses_saved_pi_recipe_and_both_existing_leases(self):
        app=self.ready();self.a.episode['name']='unsaved draft';self.a.episode['steps']=[]
        owner=object.__new__(PcChainExecution);owner.command='PC_BUILD_LOAD_B';owner.manager=self.manager
        request=owner.make_request(app,apply_jig=True)
        self.assertEqual(request['chain_command'],'PC_BUILD_LOAD_B');self.assertEqual(request['arm'],'arm2')
        self.assertEqual(set(request['links']),{'arm2','arm3'});self.assertNotIn('bundle',request);self.assertNotIn('steps',request['episode'])
        request['execution_id']='a'*32
        validate(SimpleNamespace(INSPECTION_PROTOCOL=6),request)
        for key,value in [('chain_command','BUILD_LOAD_B'),('apply_jig',False),('inspection_criteria_sha256','0'*64)]:
            bad={**request,key:value}
            with self.assertRaises(ValueError):validate(SimpleNamespace(INSPECTION_PROTOCOL=6),bad)
    def test_disabled_config_stops_before_camera_or_device_claim(self):
        app=self.ready()
        with patch('so101_teach.pc_chain_execution.configuration',side_effect=ValueError('offline validation')):
            with self.assertRaisesRegex(ValueError,'offline'):PcChainExecution(app,'PC_BUILD_LOAD_A')
        self.a.remote.rpc.assert_not_called();self.b.remote.rpc.assert_not_called()
    def test_chain_cancel_holds_both_leases_after_handoff(self):
        self.ready();owner=object.__new__(PcChainExecution)
        owner.request={'links':{}};owner.links=[(key,app,app.remote,app.session) for key,app in self.manager.apps.items()]
        owner.hold()
        for app in (self.a,self.b):app.remote.rpc.assert_called_once_with('command',{'action':'hold'})
