import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from so101_teach.domain import ROOT

spec=importlib.util.spec_from_file_location('linear_pwm',ROOT/'tools/linear_pwm.py')
pwm=importlib.util.module_from_spec(spec);spec.loader.exec_module(pwm)


class HardwarePWMTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.chip=self.root/'pwmchip7'
        (self.chip/'device').mkdir(parents=True)
        (self.root/'pwm@98000').mkdir()
        (self.chip/'device/of_node').symlink_to(self.root/'pwm@98000',target_is_directory=True)
        (self.chip/'device/of_node/compatible').write_bytes(b'raspberrypi,rp1-pwm\0')
        (self.chip/'npwm').write_text('4')
        self.channel=self.chip/'pwm2';self.channel.mkdir()
        for name,value in {'period':'20000000','polarity':'normal','enable':'1','duty_cycle':'0'}.items():
            (self.channel/name).write_text(value)
    def test_finds_rp1_by_identity_and_does_not_replay_at_start(self):
        driver=pwm.HardwarePWM(self.root)
        self.assertEqual(driver.path,self.channel)
        self.assertEqual((self.channel/'duty_cycle').read_text(),'0')
        driver.close()
        self.assertEqual((self.channel/'duty_cycle').read_text(),'0')
    def test_only_duty_changes_and_zero_stays_enabled_for_defined_low_level(self):
        driver=pwm.HardwarePWM(self.root)
        driver.write(1015);self.assertEqual((self.channel/'duty_cycle').read_text(),'1015000')
        driver.write(2000);self.assertEqual((self.channel/'duty_cycle').read_text(),'2000000')
        driver.close();self.assertEqual((self.channel/'duty_cycle').read_text(),'0')
        self.assertEqual((self.channel/'period').read_text(),'20000000')
        self.assertEqual((self.channel/'enable').read_text(),'1')
    def test_invalid_pulses_cannot_change_output(self):
        driver=pwm.HardwarePWM(self.root)
        for pulse in (-1,999,2001,True,1000.0):
            with self.assertRaises(ValueError):driver.write(pulse)
        self.assertEqual((self.channel/'duty_cycle').read_text(),'0')
    def test_unprepared_or_active_channel_is_rejected(self):
        for name,value in {'period':'1000000','enable':'0','polarity':'inversed','duty_cycle':'1015000'}.items():
            path=self.channel/name;old=path.read_text();path.write_text(value)
            with self.assertRaises(RuntimeError):pwm.HardwarePWM(self.root)
            path.write_text(old)
    def test_wrong_chip_does_not_fall_back_to_software_pwm(self):
        (self.chip/'device/of_node/compatible').write_bytes(b'brcm,bcm2835-pwm\0')
        with self.assertRaises(RuntimeError):pwm.HardwarePWM(self.root)
    def test_fan_pwm1_is_never_accepted_as_header_pwm0(self):
        (self.chip/'device/of_node').unlink()
        fan=self.root/'pwm@9c000';fan.mkdir()
        (fan/'compatible').write_bytes(b'raspberrypi,rp1-pwm\0')
        (self.chip/'device/of_node').symlink_to(fan,target_is_directory=True)
        with self.assertRaises(RuntimeError):pwm.HardwarePWM(self.root)
    def test_first_export_sets_period_before_any_other_kernel_state_change(self):
        (self.channel/'period').write_text('0')
        (self.channel/'enable').write_text('0')
        write=Path.write_text
        def kernel_write(path,value,*args,**kwargs):
            if path.name!='period' and int((path.parent/'period').read_text())==0:
                raise OSError(22,'Invalid argument')
            return write(path,value,*args,**kwargs)
        with patch.object(Path,'write_text',kernel_write):pwm.initialize_channel(self.channel)
        self.assertEqual((self.channel/'duty_cycle').read_text(),'0')
        self.assertEqual((self.channel/'enable').read_text(),'1')
        pwm.HardwarePWM(self.root)
