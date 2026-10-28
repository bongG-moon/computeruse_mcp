"""In-memory observation projection cost; does not measure UIA or business time."""
import argparse
import copy
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inspection import inspect_window


class Runtime:
    mode = 'uia'
    def __init__(self, rows):
        self.rows, self.calls = rows, 0
    def check_active(self): pass
    def call(self, name, args):
        assert name == 'get_window_state'
        self.calls += 1
        return {'structuredContent': {'pid': 11, 'window_id': 22, 'elements': copy.deepcopy(self.rows)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = [{'element_index': 0, 'role': 'Window', 'name': 'Fixture'},
            {'element_index': 1, 'parent_index': 0, 'role': 'ToolBar', 'automation_id': 'tsViewCommand'},
            {'element_index': 2, 'parent_index': 0, 'role': 'Pane', 'automation_id': 'psView'}]
    rows += [{'element_index': i+3, 'parent_index': 1 if i<20 else 2, 'role': 'Button',
              'name': 'Action '+str(i), 'actions': ['invoke']} for i in range(597)]
    names = {'generic': 'fixture.exe', 'smartfx': 'SKhynix.SmartFX.PNTLauncher.exe',
             'menuagent': 'SKhynix.SmartFX.ClientFX.MenuAgentHost.4.0.exe'}
    samples = {name: [] for name in names}; counts = {name: [] for name in names}
    for iteration in range(11):
        for name, executable in names.items():
            runtime = Runtime(rows); tick = time.perf_counter()
            inspect_window(runtime, {'pid': 11, 'window_id': 22}, observation='uia',
                           max_controls=200, host_executable=executable)
            elapsed = (time.perf_counter()-tick)*1000
            if iteration:
                samples[name].append(elapsed); counts[name].append(runtime.calls)
    result = {'scope': 'synthetic in-memory 600 nodes, 200 returned controls; excludes real Driver and application latency',
              'measurements': {name: {'p50_ms': round(statistics.median(values), 3),
                  'p95_ms': round(max(values), 3), 'repetitions': len(values),
                  'driver_calls_per_observation': sorted(set(counts[name]))} for name, values in samples.items()},
              'extra_model_calls': 0, 'extra_diagnostic_traversals': 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
