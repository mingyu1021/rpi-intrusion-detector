import random
import time

SERVO = 18 # GPIO18
TRIG = 20 # GPIO20
ECHO = 16 # GPIO16
LED = 6 # GPIO6

MAX_RANGE_CM = 400 # HC-SR04 측정 한계
ECHO_TIMEOUT = 0.03 # 약 5m 왕복 시간


# 실제 서보, 초음파 센서, LED를 다루는 클래스
class Hardware:
    def __init__(self):
        import RPi.GPIO as GPIO
        self.GPIO = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(TRIG, GPIO.OUT, initial=0)
        GPIO.setup(ECHO, GPIO.IN)
        GPIO.setup(LED, GPIO.OUT, initial=0)
        GPIO.setup(SERVO, GPIO.OUT)
        self.pwm = GPIO.PWM(SERVO, 50) # 서보 모터는 50Hz 신호 사용
        self.pwm.start(0)
        self.angle = None

    def move(self, angle):
        travel = 180 if self.angle is None else abs(angle - self.angle)
        self.pwm.ChangeDutyCycle(2.5 + angle / 18) # 0도=2.5%, 180도=12.5%
        time.sleep(0.25 + travel / 180 * 0.5) # 서보가 움직일 시간
        self.pwm.ChangeDutyCycle(0) # 펄스를 멈추어 떨림 방지
        time.sleep(0.1) # 센서의 흔들림이 멎을 시간
        self.angle = angle

    def _wait_echo(self, level):
        deadline = time.monotonic() + ECHO_TIMEOUT
        while self.GPIO.input(ECHO) != level:
            if time.monotonic() > deadline:
                return None
        return time.monotonic()

    def read_cm(self):
        # 이전 측정의 echo가 끝나지 않았으면 이번 측정은 버림
        if self._wait_echo(0) is None:
            return None
        self.GPIO.output(TRIG, 1)
        time.sleep(0.00001)
        self.GPIO.output(TRIG, 0)
        start = self._wait_echo(1)
        if start is None:
            return None
        end = self._wait_echo(0)
        if end is None:
            return None
        distance = (end - start) * 34300 / 2
        return distance if 2 <= distance <= MAX_RANGE_CM else None

    def led(self, on):
        self.GPIO.output(LED, 1 if on else 0)

    def close(self):
        self.led(False)
        self.pwm.stop()
        del self.pwm # cleanup 뒤 소멸자가 닫힌 GPIO에 접근하지 않도록 먼저 해제
        self.GPIO.cleanup()


# 하드웨어 없이 화면과 감지 로직을 확인하기 위한 가상 센서
# 시작 30초 후부터 60초마다 15초 동안 침입자가 나타남
class SimHardware:
    def __init__(self):
        self.started = time.monotonic()

    def move(self, angle):
        time.sleep(0.05)

    def read_cm(self):
        elapsed = time.monotonic() - self.started
        intruder = elapsed > 30 and (elapsed - 30) % 60 < 15
        return (70 if intruder else 180) + random.uniform(-1, 1)

    def led(self, on):
        pass

    def close(self):
        pass
