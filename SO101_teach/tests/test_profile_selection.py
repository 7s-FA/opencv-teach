import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_ui as fixtures
from so101_teach.domain import read_json


class ProfileSelectionTests(unittest.TestCase):
    setUp=fixtures.UITests.setUp
    tearDown=fixtures.UITests.tearDown
    def choose_new(self):
        panel=self.app.settings;profile=deepcopy(self.app.profile)
        profile['name']='선택 즉시 불러오기';profile['grip_contact_load']=91
        key=panel.library.save(profile['name'],profile)
        panel.refresh_profiles();panel.profile_choice.current(panel.profile_keys.index(key))
        return panel,key

    def test_combobox_selection_loads_settings_without_apply_button(self):
        panel,key=self.choose_new();panel.profile_choice.event_generate('<<ComboboxSelected>>');self.root.update()
        self.assertEqual(self.app.profile['name'],'선택 즉시 불러오기')
        self.assertEqual(panel.vars['grip_contact_load'].get(),'91')
        self.assertEqual(read_json(self.data/'profile.json')['grip_contact_load'],91)
        self.assertEqual(panel.active_profile_key,key)

    def test_blocked_selection_restores_visible_choice(self):
        panel,key=self.choose_new();previous=panel.active_profile_key;before=deepcopy(self.app.profile)
        self.app.session=SimpleNamespace(running=True)
        try:
            panel.profile_choice.event_generate('<<ComboboxSelected>>')
            self.assertEqual(panel.active_profile_key,previous)
            self.assertEqual(panel.profile_keys[panel.profile_choice.current()],previous)
            self.assertEqual(self.app.profile,before)
        finally:self.app.session=None

    def test_other_arm_selection_routes_to_workspace_once(self):
        panel,key=self.choose_new();panel.library.items[key]['robot_id']='arm3'
        manager=Mock();self.app.workspace_manager=manager
        try:
            panel.profile_choice.event_generate('<<ComboboxSelected>>')
            manager.select.assert_called_once_with('arm3')
        finally:self.app.workspace_manager=None
