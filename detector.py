import csv
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Config:
    arm_delay: float = 30 # 감시 시작 전, 사람이 나갈 시간(초)
    learn_samples: int = 25 # 기준 거리를 학습할 측정 횟수
    samples: int = 3 # 판단 한 번에 측정할 횟수
    sample_gap: float = 0.06 # 초음파 측정 사이 간격(초)
    confirm_samples: int = 3 # 이상이 보일 때 다시 측정할 횟수
    confirm_needed: int = 2 # 재측정 중 이상이어야 하는 횟수
    min_tolerance_cm: float = 5 # 측정 오차로 볼 최소 거리
    alarm_hold: float = 10 # 마지막 감지 후 경보를 유지할 시간(초)
    rest_angle: int = 0 # 평소 서보 깃발 각도
    alarm_angle: int = 90 # 경보 때 서보 깃발 각도
    missed_limit: int = 20 # 연속 무응답이 이 횟수를 넘으면 경고
    history_size: int = 240 # 화면에 보낼 거리 기록 개수


@dataclass
class Cell:
    baseline: float # 기준 거리
    tolerance: float
    closer_only: bool # 측정이 불안정해 가까워진 경우만 침입으로 판단


def median_or_none(readings):
    valid = [r for r in readings if r is not None]
    return statistics.median(valid) if valid else None


def learn_cell(readings, cfg):
    valid = [r for r in readings if r is not None]
    # 학습 중 대부분 응답이 없으면 정상 상태를 알 수 없음
    if len(valid) < len(readings) / 2:
        return None
    spread = max(valid) - min(valid)
    tolerance = max(cfg.min_tolerance_cm, spread * 2)
    return Cell(statistics.median(valid), tolerance, len(valid) < len(readings))


def deviates(cell, distance):
    if distance is None: # 초음파가 돌아오지 않은 것은 침입의 근거로 삼지 않음
        return False
    change = distance - cell.baseline
    if change < -cell.tolerance:
        return True
    return change > cell.tolerance and not cell.closer_only


class _Cancelled(Exception):
    pass


class Detector:
    LED_PATTERNS = { # 상태별 LED (켜짐, 꺼짐) 시간
        "countdown": (0.5, 0.5),
        "learning": (0.2, 0.8),
        "armed": (0.05, 1.95),
        "alarm": (0.1, 0.1),
    }

    def __init__(self, hw, cfg=None, log_path="events.csv"):
        self.hw = hw
        self.cfg = cfg or Config()
        self.log_path = log_path
        self.lock = threading.Lock()
        self.state = "idle" # idle, countdown, learning, armed, alarm, error
        self.message = ""
        self.warning = ""
        self.countdown_until = 0
        self.alarm_until = 0
        self.cell = None
        self.distance = None
        self.history = deque(maxlen=self.cfg.history_size)
        self.events = self._load_events()
        self.arm_request = threading.Event()
        self.cancel = threading.Event()
        self.closing = threading.Event()
        self.threads = [
            threading.Thread(target=self._run, daemon=True),
            threading.Thread(target=self._led_loop, daemon=True),
        ]

    def start(self):
        for thread in self.threads:
            thread.start()

    def close(self):
        self.closing.set()
        self.cancel.set()
        self.arm_request.set()
        for thread in self.threads:
            thread.join(timeout=3)

    def arm(self):
        with self.lock:
            if self.state not in ("idle", "error"):
                return False
            self.state = "countdown"
            self.message = ""
            self.warning = ""
            self.countdown_until = time.monotonic() + self.cfg.arm_delay
            self.cell = None
            self.distance = None
            self.history.clear()
        self.cancel.clear()
        self.arm_request.set()
        return True

    def disarm(self):
        self.cancel.set()

    def snapshot(self):
        with self.lock:
            cell = self.cell
            return {
                "state": self.state,
                "message": self.message,
                "warning": self.warning,
                "countdown": max(0, round(self.countdown_until - time.monotonic())),
                "distance": self.distance,
                "baseline": cell.baseline if cell else None,
                "tolerance": cell.tolerance if cell else None,
                "history": list(self.history),
                "events": self.events[-20:][::-1],
            }

    def _set(self, **values):
        with self.lock:
            for name, value in values.items():
                setattr(self, name, value)

    def _pause(self, seconds):
        if self.cancel.wait(seconds):
            raise _Cancelled

    def _sample(self, count):
        readings = []
        for _ in range(count):
            self._pause(self.cfg.sample_gap)
            readings.append(self.hw.read_cm())
        return readings

    def _record(self, distance):
        with self.lock:
            self.distance = distance
            self.history.append({"time": time.time(), "distance": distance})

    def _learn(self):
        readings = self._sample(self.cfg.learn_samples)
        cell = learn_cell(readings, self.cfg)
        if cell is None:
            raise RuntimeError("초음파 센서 응답이 부족합니다. 4m 안의 물체를 향하게 하고 배선을 확인하세요.")
        self._set(cell=cell)

    def _alarm(self, distance):
        with self.lock:
            new_alarm = self.state != "alarm"
            self.state = "alarm"
            self.alarm_until = time.monotonic() + self.cfg.alarm_hold
            if not new_alarm:
                return
            event = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "distance": round(distance, 1),
                "baseline": round(self.cell.baseline, 1),
            }
            self.events.append(event)
        self._save_event(event)
        self.hw.move(self.cfg.alarm_angle)

    def _check(self):
        distance = median_or_none(self._sample(self.cfg.samples))
        self._record(distance)
        if deviates(self.cell, distance):
            # 순간적인 튀는 값을 거르기 위해 다시 측정
            confirmed = self._sample(self.cfg.confirm_samples)
            hits = [r for r in confirmed if deviates(self.cell, r)]
            if len(hits) >= self.cfg.confirm_needed:
                self._alarm(statistics.median(hits))
        return distance

    def _watch(self):
        self._set(state="armed")
        missed = 0
        while True:
            missed = 0 if self._check() is not None else missed + 1
            self._set(warning="초음파 센서 응답이 없습니다." if missed > self.cfg.missed_limit else "")
            with self.lock:
                recovered = self.state == "alarm" and time.monotonic() > self.alarm_until
                if recovered:
                    self.state = "armed"
            if recovered:
                self.hw.move(self.cfg.rest_angle)

    def _session(self):
        self._pause(self.cfg.arm_delay)
        self._set(state="learning")
        self._learn()
        self._watch()

    def _run(self):
        self.hw.move(self.cfg.rest_angle)
        while True:
            self.arm_request.wait()
            self.arm_request.clear()
            if self.closing.is_set():
                return
            try:
                self._session()
            except _Cancelled:
                self._set(state="idle")
            except Exception as error:
                self._set(state="error", message=str(error))
            self.hw.move(self.cfg.rest_angle)

    def _led_loop(self):
        while not self.closing.is_set():
            pattern = self.LED_PATTERNS.get(self.state)
            if pattern is None:
                self.hw.led(False)
                self.closing.wait(0.1)
                continue
            self.hw.led(True)
            self.closing.wait(pattern[0])
            self.hw.led(False)
            self.closing.wait(pattern[1])

    def _load_events(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def _save_event(self, event):
        is_new = not os.path.exists(self.log_path)
        with open(self.log_path, "a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=event.keys())
            if is_new:
                writer.writeheader()
            writer.writerow(event)
