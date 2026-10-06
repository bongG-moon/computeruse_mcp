"""Explicit native administrator acceptance on synthetic documents only.

--normal exercises the authenticated bridge without requesting elevation.
Default mode requests Windows runas/UAC and never silently retries as normal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from live_validation import Client, decoded
from settings import default_config, save_config
from build_portable import runtime_environment

HERE = Path(__file__).resolve().parent
FIXTURE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Security.Principal;
using System.Web.Script.Serialization;
using System.Windows.Forms;
class AdminFixture {
    [STAThread] static void Main() {
        var form = new Form(); form.Text = "CUA 관리자권한 합성시험"; form.Width = 600; form.Height = 260;
        var input = new TextBox(); input.Name = "AdminInput"; input.AccessibleName = "시험 입력";
        input.SetBounds(30,30,480,30); form.Controls.Add(input);
        var button = new Button(); button.Name="AdminApply"; button.AccessibleName="적용"; button.Text="적용";
        button.SetBounds(30,80,120,40); form.Controls.Add(button);
        var close=new Button();close.Name="AdminClose";close.AccessibleName="시험 창 종료";close.Text="시험 창 종료";
        close.SetBounds(200,80,160,40);form.Controls.Add(close);close.Click+=delegate{form.Close();};
        var output = new TextBox(); output.Name="AdminOutput"; output.AccessibleName="확인 결과"; output.ReadOnly=true;
        output.SetBounds(30,145,480,30); form.Controls.Add(output);
        int clicks=0;
        Action save = delegate {
            var identity=WindowsIdentity.GetCurrent(); var principal=new WindowsPrincipal(identity);
            var receipt=new {synthetic_fixture=true,administrator=principal.IsInRole(WindowsBuiltInRole.Administrator),
                pid=Process.GetCurrentProcess().Id,session=Process.GetCurrentProcess().SessionId,input=input.Text,output=output.Text,clicks=clicks};
            File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"fixture-result.json"),
                new JavaScriptSerializer().Serialize(receipt),new System.Text.UTF8Encoding(false));
        };
        input.TextChanged+=delegate{save();};
        button.Click+=delegate{clicks++;output.Text=input.Text;save();};
        form.Shown+=delegate{save();}; Application.Run(form);
    }
}
'''


def run(bundle: Path, driver: Path, folder: Path, normal=False) -> dict:
    owned_root = (HERE / '.data/administrator-validation').resolve()
    folder = folder.resolve()
    if not folder.is_relative_to(owned_root) or folder == owned_root or folder.exists():
        raise ValueError('Use a new child folder under computer-use-mcp/.data/administrator-validation')
    folder.mkdir(parents=True)
    source, executable = folder / 'AdminFixture.cs', folder / 'AdminFixture.exe'
    source.write_text(FIXTURE, encoding='utf-8')
    compiler = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/codepage:65001',
                    '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
                    '/out:' + str(executable), str(source)], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    config = default_config(folder / 'config.json')
    config.update(driver=str(driver.resolve()), approval='client', state_dir=str(folder / 'state'),
                  programs=[{'id':'admin-fixture','name':'관리자권한 합성시험','exe':str(executable),
                             'enabled':True,'control_exes':[],'hints':'합성 자료만 사용합니다.'}])
    save_config(folder / 'config.json', config)
    command = [str(bundle / 'Computer Use MCP 관리자 연결.exe'), '--config', str(folder / 'config.json')]
    if normal:
        command.append('--normal')
    report = {'passed': False, 'normal_mode': normal, 'llm_used': False, 'user_apps_used': False}
    client = None
    try:
        client = Client(folder / 'config.json', command=command, initialize_timeout=120,
                        env=runtime_environment(bundle / 'runtime'))
        status = decoded(client.request('tools/call', {'name':'computer_status','arguments':{}}))
        report['execution'] = status['execution']
        if not normal and status['execution']['administrator'] is not True:
            raise AssertionError('Requested administrator execution was not verified')
        report['tools'] = len(client.request('tools/list')['tools'])
        def call(name, args):
            value = client.request('tools/call', {'name':name,'arguments':args}, timeout=45)
            if value.get('isError'):
                raise AssertionError(name + ': ' + str(decoded(value)))
            return decoded(value)
        call('computer_begin', {'program_ids':['admin-fixture'],'task_description':'합성 입력 후 실제 종료 확인'})
        launched = call('computer_launch', {'program_id':'admin-fixture'})
        report['launch'] = launched
        receipt_path = folder / 'fixture-result.json'
        deadline = time.monotonic() + 15
        while not receipt_path.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        pid = receipt['pid']
        if not normal and receipt['administrator'] is not True:
            raise AssertionError('Synthetic target did not inherit administrator execution')
        windows = call('list_windows', {'pid':pid})
        (folder / 'windows.json').write_text(json.dumps(windows,ensure_ascii=False,indent=2),encoding='utf-8')
        candidates = [row for row in windows.get('windows', windows.get('items', []))
                      if row.get('title') == 'CUA 관리자권한 합성시험' and row.get('pid') == pid]
        if len(candidates) != 1:
            raise AssertionError('Exact synthetic target window is ambiguous')
        row = candidates[0]
        target = {'pid':pid,'window_id':row.get('window_id',row.get('id'))}
        inspect = call('computer_inspect', target)
        (folder/'inspection.json').write_text(json.dumps(inspect,ensure_ascii=False,indent=2),encoding='utf-8')
        text = '관리자 연결 검증 ABC 123'
        mutation = call('computer_perform', {**target,'delivery_mode':'background',
            'step':{'operation':'set_value','selector':{'name':'시험 입력','role':'Edit'},'value':text,
                    'expect':[{'selector':{'name':'시험 입력','role':'Edit'},'property':'value','equals':text}]}})
        report['input_verified'] = mutation['task_verified']
        applied = call('computer_perform', {**target,'delivery_mode':'background',
            'step':{'operation':'click','selector':{'name':'적용','role':'Button'},
                    'expect':[{'selector':{'name':'확인 결과','role':'Edit'},'property':'value','equals':text}]}})
        report['button_verified'] = applied['task_verified']
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        report['independent_receipt'] = receipt
        assert receipt['input'] == text and receipt['output'] == text and receipt['clicks'] == 1
        closed = call('computer_close', {**target,'scope':'process','delivery_mode':'background',
                                        'close_action':{'operation':'click','selector':{'name':'시험 창 종료','role':'Button'}},'timeout_ms':3000})
        if closed['task_verified'] is not True:
            raise AssertionError('Actual synthetic process exit was not verified')
        report['process_exit_verified'] = True
        call('computer_end', {})
        report['passed'] = True
        return report
    finally:
        if client:
            client.close()
        (folder/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',required=True,type=Path)
    parser.add_argument('--driver',required=True,type=Path)
    parser.add_argument('--normal',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    folder=args.output or HERE/'.data/administrator-validation'/('run-'+uuid.uuid4().hex[:8])
    print(json.dumps(run(args.bundle.resolve(),args.driver,folder,args.normal),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
