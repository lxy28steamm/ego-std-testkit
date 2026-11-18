"""Generate reviewable interface test cases from a captured OpenAPI definition."""
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / 'docs'
SPEC = DOCS / '香橙派-OpenAPI-D-o-0.0.29.json'
BASE = 'http://192.168.50.25:18000'
METHODS = {'get', 'post', 'put', 'patch', 'delete'}


def schema_type(schema):
    if '$ref' in schema:
        return schema['$ref'].rsplit('/', 1)[-1]
    if 'anyOf' in schema:
        return '/'.join(schema_type(s) for s in schema['anyOf'])
    return schema.get('type', '未声明')


def conditions(path):
    rules = []
    if '/umi/' in path:
        rules.append('UMI 已启用并连接；按需准备左右手、相机或 VIVE')
    elif '/dex/' in path:
        rules.append('DEX 已启用并连接；按需准备手侧及相机')
    elif '/camera/' in path or '/ego-plus/' in path:
        rules.append('EGO 对应型号已启用并连接')
    if any(s in path for s in ('/cloud/', '/task-folders', '/collection-task/assigned')):
        rules.append('使用测试账号及测试任务，确认权限和网络')
    if any(s in path for s in ('/files', '/datasets', '/download')):
        rules.append('准备可识别的测试文件并确认权限')
    if '/session/' in path or '/collection-task' in path:
        rules.append('确认当前会话空闲且测试目标唯一')
    if '/calibration' in path or '/firmware' in path or '/software-update' in path:
        rules.append('仅在专项测试设备上准备可恢复方案和测试资料')
    if '/network/wifi' in path:
        rules.append('确保设备网络可达；Wi-Fi 查询建议至少等待 20 秒')
    return '；'.join(rules) if rules else '设备在线，确认当前版本和访问权限'


def verify(path, method):
    if 'start' in path and '/session/' in path:
        return '核对本轮 session_id 和录制状态，随后按专项流程停止并检查本轮 MCAP'
    if 'stop' in path and '/session/' in path:
        return '核对仅本轮会话停止、文件完成收尾及对应 MCAP；不操作其他会话'
    if 'upload' in path:
        return '核对测试数据、任务 ID、上传任务状态及平台结果；文件完整性须另行核验'
    if '/calibration' in path:
        return '核对设备型号、手侧、标定数据及读回结果；参数有效性以标定规范为准'
    if 'firmware' in path or 'software-update' in path:
        return '记录升级前后版本、设备身份和恢复结果；升级功能需专项验收'
    if '/network' in path:
        return '核对目标网卡与设备实际网络状态；勿以连接状态代替网络稳定性测试'
    if '/storage' in path:
        return '核对存储类型、路径及可用空间与设备实际一致'
    if '/health' in path:
        return '检查整体状态、目标设备、运行状态与设备实际情况一致'
    if '/preview' in path or 'preview' in path:
        return '核对画面来源和新鲜度；仅接口响应不能证明预览流畅'
    if method == 'get':
        return '检查返回字段、类型和目标资源与实际状态一致'
    if method == 'delete':
        return '仅对隔离的测试目标操作，并核对删除结果和其他数据不受影响'
    return '核对操作目标、响应和状态读回；与设备实际行为及产品规范比较'


def main():
    spec = json.loads(SPEC.read_text(encoding='utf-8'))
    operations = [(m, path, op) for path, entries in spec['paths'].items()
                  for m, op in entries.items() if m.lower() in METHODS]
    lines = [
        '# 香橙派 HTTP 接口测试用例（设计稿）', '',
        f'- 基础地址：`{BASE}`；OpenAPI 版本：`{spec["info"]["version"]}`；来源：本地设备接口定义快照。',
        f'- 覆盖 {len(operations)} 个方法与路径；每项包含正常和异常场景。所有用例均为**未执行**，具体通过标准、设备配置、凭据、测试数据需按产品规范补齐。',
        '- 实际参数、请求体、响应模型以配套 OpenAPI 和《香橙派设备接口文档》为准；`{...}` 为路径参数。',
        '- 不批量运行写操作：切网、删除、升级、标定、采集、上传和登录切换可能影响设备或数据，需隔离测试数据并确认恢复路径。',
        '- 接口定义存在不等于当前设备支持；不同型号、权限和硬件状态下可能不可用，无法判定的项目标记未测或待确认。',
        '- 正常场景按契约接受响应并验证业务状态；异常场景以已确认的接口契约为准，不把未经证实的 4xx 错误码当标准。',
        '', '## 执行前的公共准备', '',
        '1. 记录日期、设备 IP、整机 SN、软件版本、接入设备型号、硬件和网络条件。',
        '2. 确认接口版本、账号权限、测试资源与备份；保存请求方法/路径、响应状态、耗时和必要的脱敏证据。',
        '3. 检查接口定义的 200、422 等只是文档声明，实际业务通过条件须与产品规范对照。',
        '4. 写操作需指定测试目标并准备清理和恢复；不对历史数据或正在运行的其他会话执行。',
        '', '## 按接口列出的用例', ''
    ]
    for number, (method, path, op) in enumerate(operations, 1):
        ps = [x for x in op.get('parameters', []) if x.get('name') != 'ego_v2_session']
        detail = []
        for p in ps:
            s = p.get('schema', {})
            item = f"`{p['name']}`（{p.get('in', '?')}，{'必填' if p.get('required') else '可选'}，{schema_type(s)}"
            if 'enum' in s:
                item += '，允许值 '+json.dumps(s['enum'], ensure_ascii=False)
            detail.append(item+'）')
        body = op.get('requestBody', {})
        for media, item in body.get('content', {}).items():
            detail.append(f"请求体 `{media}`（{'必填' if body.get('required') else '可选'}，模型 `{schema_type(item.get('schema', {}))}`）")
        paramtext = '；'.join(detail) or '无显式业务参数'
        if ps:
            p = next((x for x in ps if x.get('required')), ps[0])
            negative = f"使用无效、越界或不存在的 `{p['name']}`，按接口契约检查错误和状态未被误改"
        elif body:
            negative = '以缺少必填字段或错误类型的隔离测试请求体验证输入校验，检查错误和状态未被误改'
        else:
            negative = '在测试环境验证无权限或前置条件不满足时的行为；具体错误码和权限要求以产品规范为准'
        expected_codes = ', '.join(op.get('responses', {}).keys()) or '未声明'
        lines += [f'### API-{number:03d} {method.upper()} `{path}`', '',
                  f"- 功能：{op.get('summary', '未提供')}。",
                  f'- 前置条件：{conditions(path)}。',
                  f'- 参数：{paramtext}；具体字段及约束参见接口文档。',
                  f'- 正常场景：使用有效测试数据发起请求，检查契约响应（文档列出 {expected_codes}）；{verify(path, method)}。',
                  f'- 异常场景：{negative}。',
                  '- 结果：未执行；记录实际状态码、响应、业务结果与证据。', '']
    lines += ['## 重要业务闭环（专项）', '',
              '- 采集：准备目标设备 → 确认空闲 → 新建隔离任务 → 启动 → 状态变化 → 停止 → 等待收尾 → 匹配本轮文件 → 校验 MCAP 结构、消息、时间戳与设备数据。',
              '- 上传：仅使用本轮授权测试文件和指定测试任务 → 校验绑定 → 上传 → 轮询状态 → 平台核对；平台显示完成不等于云端文件内容已校验。',
              '- 设备切换、标定写入、网络变更和升级：分别准备专用设备、恢复方案和准确标准，成功与失败路径不得影响其他设备与历史数据。',
              '', '## Apifox 可执行起步用例', '',
              '配套 `香橙派-Apifox-只读起步用例.postman_collection.json` 含 4 个只读请求及基本断言，可作为 Postman Collection 导入 Apifox；它只验证状态码及 JSON 基本类型，不能替代本完整清单。']
    md = DOCS / '香橙派接口测试用例-设计稿.md'
    md.write_text('\n'.join(lines)+'\n', encoding='utf-8')

    cases = [('/api/v2/health', '健康状态', "pm.expect(body).to.be.an('object'); pm.expect(body).to.have.property('overall_state');"),
             ('/api/v2/storage', '存储选项', "pm.expect(body).to.be.an('object'); pm.expect(body).to.have.property('current');"),
             ('/api/v2/network', '网络状态', "pm.expect(body).to.be.an('object'); pm.expect(body).to.have.property('connected');"),
             ('/api/v2/network/wifi', 'Wi-Fi 网络', "pm.expect(body).to.be.an('array');")]
    items = []
    for path, name, check in cases:
        script = ["pm.test('HTTP status is 200', function () { pm.response.to.have.status(200); });",
                  "pm.test('Response is JSON with expected basic structure', function () { var body = pm.response.json(); " + check + " });"]
        items.append({'name': name, 'request': {'method':'GET', 'header':[], 'url': {'raw':'{{base_url}}'+path, 'host':['{{base_url}}'], 'path':path.strip('/').split('/')}},
                      'event':[{'listen':'test','script':{'type':'text/javascript','exec':script}}]})
    collection = {'info':{'name':'香橙派只读接口起步测试','description':'仅检查响应码和基础结构，不代表整机通过；Wi-Fi 查询需至少 20 秒超时。','schema':'https://schema.getpostman.com/json/collection/v2.1.0/collection.json'},
                  'variable':[{'key':'base_url','value':BASE,'type':'string'}], 'item':items}
    target = DOCS / '香橙派-Apifox-只读起步用例.postman_collection.json'
    target.write_text(json.dumps(collection,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('operations:',len(operations),'normal and negative scenarios:',len(operations)*2)
    print('generated:',md, target,sep='\n')


if __name__ == '__main__':
    main()
