import argparse
import os
import signal
import sys

from flask import Flask, jsonify, render_template

from detector import Config, Detector
from hardware import Hardware, SimHardware

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
detector = None


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def status():
    return jsonify(detector.snapshot())


@app.route('/api/arm', methods=['POST'])
def arm():
    return jsonify(ok=detector.arm())


@app.route('/api/disarm', methods=['POST'])
def disarm():
    detector.disarm()
    return jsonify(ok=True)


def main():
    global detector
    parser = argparse.ArgumentParser(description="초음파 침입 감지 시스템")
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--arm-delay', type=float, default=Config.arm_delay,
                        help="감시 시작 전 대기 시간(초)")
    parser.add_argument('--sim', action='store_true', help="하드웨어 없이 가상 방으로 실행")
    args = parser.parse_args()

    try:
        hw = SimHardware() if args.sim else Hardware()
    except Exception as error:
        sys.exit(f"GPIO를 열 수 없습니다: {error}\n"
                 "같은 핀을 쓰는 다른 프로그램(예: 10-3-1mqtt.py)을 먼저 종료하세요.")

    log_name = 'events-sim.csv' if args.sim else 'events.csv'
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_name)
    detector = Detector(hw, Config(arm_delay=args.arm_delay), log_path)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    detector.start()
    try:
        app.run(host='0.0.0.0', port=args.port, threaded=True)
    finally:
        detector.close()
        hw.close()


if __name__ == "__main__":
    main()
