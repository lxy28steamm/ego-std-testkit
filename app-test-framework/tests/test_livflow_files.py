import json
import os
from pathlib import Path
import pytest


def test_recorded_mcap_integrity(pytestconfig):
    config = json.loads(Path(os.environ.get('APP_PATROL_FLOW_CONFIG',
        str(Path(__file__).parents[1]/'config/patrol.json'))).read_text(encoding='utf-8'))
    path = config.get('mcap_file','')
    if not path:
        pytest.skip('尚未选择本轮MCAP文件')
    from framework.recording_check import inspect,compare
    result = inspect(path)
    assert all(x['backward']==0 for x in result['topics'].values()), '消息时间戳存在倒退'
    if config.get('before_mcap'):
        result = compare(config['before_mcap'],path)
        assert result['messages_unchanged'], '传感器消息及时间戳摘要不一致'
        assert result['source_content_matches'], '元数据与源标定/JSON不一致'
    (pytestconfig.artifact_dir/'mcap_check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
