"""Actual MSAA COM provider test in a disposable local Windows fixture."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from build_portable import SOURCE, COMPILER, compile_msaa
from msaa_dropdown import regions

class NativeMsaaTest(unittest.TestCase):
    def test_hwndless_list_read_from_real_legacy_provider(self):
        with tempfile.TemporaryDirectory(dir=SOURCE/'.data',prefix='msaa-native-') as folder:
            root=Path(folder); helper=root/'reader.exe'; fixture=root/'fixture.exe'
            compile_msaa(helper)
            subprocess.run([str(COMPILER),'/nologo','/target:exe','/reference:System.dll','/reference:System.Drawing.dll',
                '/reference:System.Windows.Forms.dll','/reference:'+str(COMPILER.parent/'Accessibility.dll'),
                '/out:'+str(fixture),str(SOURCE/'fixtures/MsaaListFixture.cs')],check=True,capture_output=True)
            process=subprocess.Popen([str(fixture)],stdout=subprocess.PIPE,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                # Fixture has its own 12-second lifetime even if the reader fails.
                hwnd=int(process.stdout.readline())
                response=subprocess.run([str(helper)],input=json.dumps({'pid':process.pid,'window_id':hwnd})+'\n',
                    text=True,encoding='utf-8',capture_output=True,timeout=4,creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(response.returncode,0,response.stdout+response.stderr)
                data=json.loads(response.stdout)
                self.assertTrue(data['complete']); self.assertTrue(data['read_only'])
                self.assertEqual(len(data['lists']),1)
                frame=data['frames'][0]
                self.assertEqual(len(regions(data,(frame['width'],frame['height']))),1)
            finally:
                if process.poll() is None: process.terminate()
                process.wait(timeout=3); process.stdout.close()

if __name__=='__main__': unittest.main()
