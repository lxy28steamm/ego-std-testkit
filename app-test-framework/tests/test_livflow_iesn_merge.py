"""10-29 两条新增功能的本地校验用例（不依赖手机）。

1. MCAP 中写入设备 sn 码：确认 livflow.device.v1 与 capture_meta 的 device.serial_number 一致，
   且错误 sn 必须判失败（反例保护，防止"怎么都能过"）。
2. 支持多条 mcap 合并上传：确认合并结果的 topic 计数等于各源之和、时间戳不倒退、
   且拿单条冒充合并结果必须判失败。

没有给出 mcap 路径时整体跳过，不伪造数据。
"""
import json
import os
from pathlib import Path

import pytest

from framework.merge_check import inventory, verify_iesn, verify_merge


def _config():
    path = os.environ.get('APP_PATROL_FLOW_CONFIG',
                          str(Path(__file__).parents[1] / 'config/patrol.json'))
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _sources(config):
    paths = config.get('merge_source_mcaps') or []
    return [Path(p) for p in paths if Path(p).is_file()]


def test_iesn_written_into_single_mcap():
    config = _config()
    sn = config.get('device_sn', '')
    paths = config.get('iesn_mcap') or config.get('mcap_file') or ''
    if not sn or not paths or not Path(paths).is_file():
        pytest.skip('未指定本轮 MCAP 或设备 SN，无法核对写入的 sn')
    one = inventory(paths)
    ok, rows = verify_iesn(one, sn)
    assert ok, 'MCAP 中未写入本轮设备 sn：' + '；'.join('%s=%s' % (r['item'], r['value'])
                                                  for r in rows if not r['ok'])
    # 反例：换一个 sn 必须判失败，防止校验恒真
    wrong, _ = verify_iesn(one, sn[:-1] + ('0' if sn[-1] != '0' else '1'))
    assert not wrong, '校验函数对错误 sn 也返回通过，判定逻辑无效'


def test_merge_counts_equal_sum_of_sources():
    merged = _config().get('merged_mcap', '')
    sources = _sources(_config())
    if not merged or not Path(merged).is_file() or len(sources) < 2:
        pytest.skip('未指定合并后文件或来源不足 2 条')
    result = verify_merge(sources, merged)
    assert result['counts_match'], ('合并后各 topic 消息数不等于来源之和：期望 %s，实际 %s'
                                    % (result['expected_topic_counts'], result['actual_topic_counts']))
    assert result['timestamp_backward'] == 0, '合并后存在时间戳倒退 %d 处' % result['timestamp_backward']


def test_single_file_cannot_pass_as_merged():
    sources = _sources(_config())
    if len(sources) < 2:
        pytest.skip('来源不足 2 条，无法做反例')
    result = verify_merge(sources, sources[0])
    assert not result['counts_match'], '单条文件也能被判为合并结果，判定逻辑无效'
