import unittest
from device_config import tuner_budget,broadcast_counts


class DeviceConfigurationTest(unittest.TestCase):
    def test_budget_uses_hardware_and_respects_smaller_device(self):
        self.assertEqual(tuner_budget('all',4),4)
        self.assertEqual(tuner_budget('2',4),2)
        self.assertEqual(tuner_budget('4',2),2)
        for value in (0,'0','-1','invalid',33):
            with self.subTest(value=value),self.assertRaises(ValueError):tuner_budget(value,4)

    def test_broadcast_counts_are_separate_from_hardware_and_device_scoped(self):
        def channel(name,selected,flags=(),device='a'):
            return {'device_id':device,'name':name,'selected':selected,'raw_data':{'source':'preset_scan','flags':flags}}
        rows=[channel('MBC',True),channel('KBS1',False),channel('Program 2',False),
              channel('Encrypted',True,['encrypted']),channel('Other',True,device='b')]
        self.assertEqual(broadcast_counts(rows,'a'),{'discovered':4,'usable':2,'selected':1})
