"""Integration checks requiring /dev/kvm; run with make check-kvm."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT/'core/minivmm'
PANEL = ROOT/'panel/vmctl.py'

@unittest.skipUnless(os.access('/dev/kvm', os.R_OK | os.W_OK), 'usable /dev/kvm required')
class KvmTests(unittest.TestCase):
    def test_hello_and_device_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            for cpus in [1, 4]:
                stats = Path(tmp)/'stats.json'
                p = subprocess.run([str(CORE),'--mem','16','--vcpus',str(cpus),'--stats',str(stats),
                                    str(ROOT/'guests/hello.bin')],capture_output=True,text=True,timeout=5)
                self.assertEqual(p.returncode,0,p.stderr)
                self.assertEqual(p.stdout, 'Hello from the guest! I am running with no OS at all.\n')
                data=json.loads(stats.read_text())
                self.assertEqual(data['exits'],{'IO':54,'HLT':cpus})
                self.assertEqual(data['total_exits'],54+cpus)
                self.assertEqual(data['state'],'halted')
            p=subprocess.run([str(CORE),str(ROOT/'guests/io_probe.bin')],capture_output=True,text=True,timeout=5)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(p.stdout,'I/O ABI OK\n')

    def test_panel_lifecycle_resize_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,VMCTL_HOME=tmp)
            def run(*args,ok=True):
                p=subprocess.run(['python3',str(PANEL),*args],env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(p.returncode==0,ok,p.stdout+p.stderr)
                return p
            run('create','burn','--guest',str(ROOT/'guests/cpu_burn.bin'),'--mem','16')
            try:
                run('run','burn')
                state=lambda:json.loads((Path(tmp)/'vms.json').read_text())['burn']
                first=state()['pid']
                self.assertTrue(Path(f'/proc/{first}').exists())  # CLI has already exited
                run('run','burn',ok=False)
                run('resize','burn','--vcpus','2',ok=False)
                self.assertEqual(state()['vcpus'],1)
                run('stats','burn','-i','0.1','-n','2')
                run('resize','burn','--vcpus','2','--restart')
                self.assertNotEqual(state()['pid'],first)
                self.assertEqual((state()['vcpus'],state()['mem_mb']),(2,16))
                run('delete','burn',ok=False)
                run('stop','burn')
                data=json.loads((Path(tmp)/'burn.stats.json').read_text())
                self.assertEqual(data['state'],'stopped')
                self.assertEqual(len(data['per_vcpu']),2)
                self.assertTrue(all(not v['running'] for v in data['per_vcpu']))
                self.assertIn('IO',data['exits'])
                run('delete','burn')
            finally:
                if (Path(tmp)/'vms.json').exists() and 'burn' in json.loads((Path(tmp)/'vms.json').read_text()):
                    run('stop','burn')

if __name__=='__main__':unittest.main()
