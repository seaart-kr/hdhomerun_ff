import time
import unittest
from types import SimpleNamespace

from hdhomerun import ScanTarget
from preset_scan_runner import PresetScanRunner


class FakeControl:
    def scan_targets(self, targets, *, tuner_count, progress_callback=None, cancelled=None):
        self.targets = targets
        self.tuner_count = tuner_count
        if progress_callback:
            progress_callback(1, len(targets), type('Scan', (), {'services': [object()]})())
        return (SimpleNamespace(target=targets[0], status=SimpleNamespace(is_locked=True), services=[object()]),)


class FakeStore:
    def __init__(self):
        self.saved = None
        self.profile = None

    def update_device_profile(self, device_id, **updates):
        self.profile = (device_id, updates)

    def replace_preset_scan_channels(self, device_id, rows):
        self.saved = (device_id, rows)
        return len(rows)


class PresetScanRunnerTest(unittest.TestCase):
    def wait_for(self, runner):
        for _ in range(100):
            state = runner.status()
            if not state['running']:
                return state
            time.sleep(0.01)
        self.fail('scan runner did not finish')

    def test_persists_only_discovery_rows_after_explicit_run(self):
        store = FakeStore()
        runner = PresetScanRunner(
            control_factory=lambda base_url: FakeControl(),
            store_factory=lambda: store,
            rows_factory=lambda device_id, scans: [{
                'channel_id': 'scan', 'device_id': device_id, 'name': 'Found',
                'source_url': '', 'enabled': False, 'raw_data': {'source': 'preset_scan'},
            }],
        )
        target = ScanTarget(0, 267000000, 'VSB/8', 'ATSC-T')
        runner.start(base_url='http://192.168.2.17', device_id='box', tuner_count=4, targets=[target])
        state = self.wait_for(runner)
        self.assertEqual(state['state'], 'complete')
        self.assertEqual(state['stored_channels'], 1)
        self.assertEqual(store.saved[0], 'box')
        self.assertFalse(store.saved[1][0]['enabled'])
        self.assertEqual(store.profile[1]['learned_muxes'], [{'frequency_hz':267000000, 'modulation':'VSB/8'}])

    def test_empty_service_result_leaves_store_untouched(self):
        called = []
        runner = PresetScanRunner(
            control_factory=lambda base_url: FakeControl(),
            store_factory=lambda: self.fail('store must not be used'),
            rows_factory=lambda device_id, scans: called.append((device_id, scans)) or [],
        )
        target = ScanTarget(0, 267000000, 'VSB/8', 'ATSC-T')
        runner.start(base_url='http://192.168.2.17', device_id='box', tuner_count=4, targets=[target])
        state = self.wait_for(runner)
        self.assertEqual(state['state'], 'complete_no_services')
        self.assertTrue(called)

    def test_cancel_before_worker_uses_results_preserves_store(self):
        class CancellingControl:
            def scan_targets(self, targets, *, tuner_count, progress_callback=None, cancelled=None):
                runner.cancel()
                return ('scan-result',)

        runner = PresetScanRunner(
            control_factory=lambda base_url: CancellingControl(),
            store_factory=lambda: self.fail('store must not be used after cancellation'),
            rows_factory=lambda device_id, scans: self.fail('rows must not be built after cancellation'),
        )
        target = ScanTarget(0, 267000000, 'VSB/8', 'ATSC-T')
        runner.start(base_url='http://192.168.2.17', device_id='box', tuner_count=4, targets=[target])
        state = self.wait_for(runner)
        self.assertEqual(state['state'], 'cancelled')
        self.assertFalse(state['running'])


if __name__ == '__main__':
    unittest.main()
