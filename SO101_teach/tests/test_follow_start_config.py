import unittest
from tests import test_ui,test_remote
from so101_teach.domain import read_json


class FollowStartUITests(unittest.TestCase):
    setUp=test_ui.UITests.setUp
    tearDown=test_ui.UITests.tearDown
    def test_initial_speed_saves_and_reloads_independently(self):
        from PIL import ImageGrab
        a=self.app;p=a.settings;a.show_page('settings');p.tabs.select(p.pages['robot'])
        p.vars['mode'].set('팔로워 단독');p.vars['follow_start_speed'].set('아주 느리게');p.save_profile()
        self.assertEqual(read_json(self.data/'profile.json')['follow_start_rate_ticks_s'],100.)
        p.vars['follow_start_speed'].set('보통');p.fill_robot();self.assertEqual(p.vars['follow_start_speed'].get(),'아주 느리게')
        for size in ('1180x760','1280x800'):
            self.root.geometry(size);self.root.update();p.scroll_canvases[str(p.pages['robot'])].yview_moveto(1);self.root.update()
            x,y=self.root.winfo_rootx(),self.root.winfo_rooty()
            ImageGrab.grab(bbox=(x,y,x+self.root.winfo_width(),y+self.root.winfo_height())).save('/tmp/follow-start-'+size+'.png')


class FollowStartRemoteTests(unittest.TestCase):
    setUp=test_remote.RemoteTests.setUp
    tearDown=test_remote.RemoteTests.tearDown
    rpc=test_remote.RemoteTests.rpc
    def test_pi_connection_receives_initial_rate_from_profile(self):
        self.bundle['profile']['follow_start_rate_ticks_s']=100.
        self.runtime.configure(self.bundle)
        result=self.rpc('connect',{'speed':350.});self.assertTrue(result['ok'])
        self.assertEqual(self.runtime.session.follow_start_rate,100.)
