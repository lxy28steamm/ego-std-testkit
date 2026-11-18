import unittest
from orangepi_acceptance import evaluate

class AcceptanceTests(unittest.TestCase):
    def health(self,device='umi'):
        return {'business':{'version':'1.0','sensor':{'state':'ready','sides':[{'side':'left','required':True,'valid':True}]},
            'health':{'cloud':{'device_sn':'board-test'},'selection':{'enabled_devices':['umi']},
                'devices':{'umi':{'active':True,'hardware_connected':True}},'storage':{'state':'healthy'},
                'session':{'session_id':'test-session','devices':[device],'result':'complete',
                    'recorder':{'final_path':'test.mcap','drop_count':0,'streams':{'/devices/umi/imu':{'message_count':10,'missing_count':0}}}}}}}
    def rows(self,data): return {r['id']:r for r in evaluate(data,'umi')}
    def test_other_device_history_is_not_umi_evidence(self):
        r=self.rows(self.health('dex'))
        for k in ('session','streams','quality'): self.assertEqual(r['ACC-'+k]['status'],'未测')
    def test_missing_api_and_unexecuted_functions_never_pass(self):
        self.assertTrue(all(r['status']=='未测' for r in evaluate({},'umi')))
        r=self.rows(self.health())
        for k in ('record','upload','display','button'): self.assertEqual(r['ACC-'+k]['status'],'未测')
        self.assertEqual(r['ACC-streams']['status'],'待确认')
    def test_faults_and_waiting_are_distinguished(self):
        d=self.health();d['business']['sensor']['state']='waiting'
        self.assertEqual(self.rows(d)['ACC-sensor']['status'],'未测')
        d['business']['health']['session']['result']='invalid'
        d['business']['health']['session']['recorder']['error_code']='worker_restart'
        r=self.rows(d)
        self.assertEqual(r['ACC-session']['status'],'失败')
        self.assertEqual(r['ACC-quality']['status'],'失败')
    def test_generic_board_cases_removed(self):
        r=self.rows(self.health())
        self.assertFalse(any(k in r for k in ('OPI-cpu','OPI-camera','OPI-accelerator')))
        self.assertEqual(r['ACC-identity']['status'],'通过')

if __name__=='__main__': unittest.main()
