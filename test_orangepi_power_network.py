import unittest
from orangepi_power_network import evaluate

class PowerNetworkTests(unittest.TestCase):
    def rows(self,data):return {x['key']:x for x in evaluate(data)}
    def test_missing_values_never_pass(self):
        self.assertTrue(all(x['status']=='未测' for x in evaluate({})))
    def test_board_current_direction_is_not_assumed(self):
        r=self.rows({'health':{'host':{'battery':{'present':True,'percent':5,'voltage_mv':12000,'current_ma':-50,'charging':True,'status':'charging'}}}})
        self.assertEqual(r['battery_level']['status'],'待确认')
        self.assertEqual(r['battery_power']['status'],'通过')
        self.assertEqual(r['charging']['status'],'通过')
    def test_dynamic_ethernet_name_and_disconnected_wifi(self):
        r=self.rows({'network':{'interfaces':[{'interface':'enP4p65s0','connection_type':'ethernet','connected':True,'ip_address':'192.0.2.1'},{'interface':'wlan0','connected':False}]}})
        self.assertEqual(r['network']['status'],'通过')
        self.assertEqual(r['ethernet']['status'],'通过')
        self.assertNotEqual(r['wifi']['status'],'通过')
    def test_invalid_battery_reading_not_passed(self):
        r=self.rows({'health':{'host':{'battery':{'present':True,'percent':float('nan'),'voltage_mv':True,'current_ma':0}}}})
        self.assertEqual(r['battery_level']['status'],'未测')
        self.assertEqual(r['battery_power']['status'],'未测')

if __name__=='__main__':unittest.main()
