import unittest,io,urllib.error,json
from unittest.mock import Mock
from orangepi_acceptance_extra import evaluate,validate_rules,EXTRA_CASES
from orangepi_login_checks import run

class ExtraTests(unittest.TestCase):
    def rows(self,api,rules=None):return {x['key']:x for x in evaluate(api,'umi',rules,now=1000)}
    def test_missing_information_is_not_passed(self):
        self.assertTrue(all(r['status']=='未测' for r in evaluate({},'umi')))
    def test_explicit_rates_missing_stream_and_wrong_device(self):
        topic='/devices/umi/control/command';rules={'streams':{topic:{'hz':200,'tolerance_ratio':.05}}}
        api={'health':{'session':{'session_id':'run','devices':['umi'],'recorder':{'streams':{topic:{'message_count':1,'actual_rate_hz':100}}}}}}
        r=self.rows(api,rules);self.assertEqual(r['stream_rates']['status'],'失败');self.assertEqual(r['stream_set']['status'],'通过')
        api['health']['session']['devices']=['dex'];self.assertEqual(self.rows(api,rules)['stream_rates']['status'],'未测')
    def test_storage_path_and_heartbeat(self):
        api={'storage':{'current':'/ssd/data','options':[{'path':'/ssd/data','available':True,'mounted':True,'free_bytes':3*1024**3}]},'health':{'cloud':{'last_heartbeat_time_ns':999*10**9}}}
        r=self.rows(api);self.assertEqual(r['storage_selection']['status'],'通过');self.assertEqual(r['storage_capacity']['status'],'通过');self.assertEqual(r['heartbeat']['status'],'通过')
        api['health']['cloud']['last_heartbeat_time_ns']=10**9;self.assertEqual(self.rows(api)['heartbeat']['status'],'待确认')
    def test_board_specific_limits_are_not_invented(self):
        api={'health':{'session':{'devices':['umi'],'recorder':{'disk_latency_ms':800}},'host':{'battery':{'voltage_mv':14000,'current_ma':-1,'charging':True}}}}
        r=self.rows(api);self.assertEqual(r['disk_latency']['status'],'未测');self.assertEqual(r['battery_limits']['status'],'未测')
    def test_invalid_configuration(self):
        for x in ({'disk_latency_max_ms':-1},{'streams':{'/devices/umi/x':{'hz':0,'tolerance_ratio':.1}}},{'battery_min_mv':17000,'battery_max_mv':13000}):
            with self.assertRaises(ValueError):validate_rules(x)
    def test_calibration_identity_is_not_geometric_validation(self):
        api={'sensor':{'sides':[{'side':'left','required':True}]},'bindings':{'mode':'dual','left_port':'/dev/a'},'calibration':{'left':{'hand':'left','port':'/dev/a','size_bytes':10,'sha256':'digest','crc32':'crc'}}}
        self.assertEqual(self.rows(api)['calibration']['status'],'待确认')
    def test_other_login_is_not_changed(self):
        api=Mock();api.call.return_value={'authenticated':True,'phone':'other'}
        self.assertTrue(all(r['status']=='未测' for r in run(api,'tester','secret')));self.assertEqual(api.call.call_count,1)
    def test_rejection_and_login_restoration(self):
        api=Mock();api.call.side_effect=[{'authenticated':True,'phone':'tester'},{'state':'idle'},{},{'authenticated':True,'account_id':1},urllib.error.HTTPError('url',401,'rejected',{},io.BytesIO(b'{}')),{}, {'authenticated':True}]
        rows=run(api,'tester','secret');self.assertTrue(all(r['status']=='通过' for r in rows));self.assertNotIn('secret',json.dumps(rows))

if __name__=='__main__':unittest.main()
