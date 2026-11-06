import os
import tempfile
import time
import unittest

from detector import Cell, Config, Detector, deviates, learn_cell

CFG = Config(arm_delay=0, sample_gap=0, alarm_hold=0.05, learn_samples=9)


class FakeHardware:
    def __init__(self, read):
        self.read = read # read(측정 순번) -> 거리
        self.reads = 0
        self.angles = []

    def move(self, angle):
        self.angles.append(angle)
        time.sleep(0.001)

    def read_cm(self):
        self.reads += 1
        time.sleep(0.0002)
        return self.read(self.reads)

    def led(self, on):
        pass


def wait_for(condition, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.002)
    return False


class CellTest(unittest.TestCase):
    def test_keeps_minimum_tolerance_for_quiet_sensor(self):
        self.assertEqual(learn_cell([20.0, 20.1, 20.2], CFG).tolerance, 5)

    def test_widens_tolerance_for_noisy_sensor(self):
        self.assertEqual(learn_cell([100, 130, 110], CFG), Cell(110, 60, False))

    def test_too_few_echoes_cannot_learn(self):
        self.assertIsNone(learn_cell([100, None, None], CFG))

    def test_missing_echo_is_not_intrusion(self):
        self.assertFalse(deviates(Cell(100, 5, False), None))

    def test_closer_and_farther_are_intrusion(self):
        cell = Cell(20, 5, False)
        self.assertTrue(deviates(cell, 14))
        self.assertTrue(deviates(cell, 26))
        self.assertFalse(deviates(cell, 23))

    def test_flaky_sensor_only_alarms_when_closer(self):
        cell = learn_cell([200, None, 200], CFG)
        self.assertFalse(deviates(cell, 300))
        self.assertTrue(deviates(cell, 100))


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.dir.name, "events.csv")

    def tearDown(self):
        self.detector.close()
        self.dir.cleanup()

    def run_detector(self, read):
        self.hw = FakeHardware(read)
        self.detector = Detector(self.hw, CFG, self.log)
        self.detector.start()
        self.assertTrue(self.detector.arm())

    def armed(self):
        return self.detector.state in ("armed", "alarm")

    def test_quiet_sensor_never_alarms(self):
        self.run_detector(lambda n: 20)
        self.assertTrue(wait_for(lambda: self.hw.reads > 200))
        self.assertEqual(self.detector.state, "armed")
        self.assertEqual(self.detector.events, [])

    def test_single_spike_is_filtered(self):
        self.run_detector(lambda n: 5 if n > 50 and n % 7 == 0 else 20)
        self.assertTrue(wait_for(lambda: self.hw.reads > 300))
        self.assertEqual(self.detector.events, [])

    def test_intruder_raises_flag_logs_once_then_recovers(self):
        present = {"on": False}
        self.run_detector(lambda n: 8 if present["on"] else 20)
        self.assertTrue(wait_for(self.armed))
        present["on"] = True
        self.assertTrue(wait_for(lambda: CFG.alarm_angle in self.hw.angles))
        present["on"] = False
        self.assertTrue(wait_for(lambda: self.detector.state == "armed"))
        self.assertTrue(wait_for(lambda: self.hw.angles[-1] == CFG.rest_angle))
        self.assertEqual(len(self.detector.events), 1)
        self.assertEqual(self.detector.events[0]["distance"], 8)
        with open(self.log, encoding="utf-8") as file:
            self.assertEqual(len(file.readlines()), 2)

    def test_dead_sensor_during_learning_is_error(self):
        self.run_detector(lambda n: None)
        self.assertTrue(wait_for(lambda: self.detector.state == "error"))
        self.assertIn("초음파", self.detector.message)

    def test_sensor_loss_while_armed_warns(self):
        self.run_detector(lambda n: 20 if n < 30 else None)
        self.assertTrue(wait_for(lambda: self.detector.warning))
        self.assertEqual(self.detector.events, [])

    def test_disarm_during_alarm_lowers_flag_and_can_rearm(self):
        self.run_detector(lambda n: 8 if n > 40 else 20)
        self.assertTrue(wait_for(lambda: self.detector.state == "alarm"))
        self.detector.disarm()
        self.assertTrue(wait_for(lambda: self.detector.state == "idle"))
        self.assertTrue(wait_for(lambda: self.hw.angles[-1] == CFG.rest_angle))
        self.assertTrue(self.detector.arm())
        self.assertTrue(wait_for(self.armed))


if __name__ == "__main__":
    unittest.main()
