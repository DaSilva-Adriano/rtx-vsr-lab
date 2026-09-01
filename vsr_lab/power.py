"""Optional nvidia-smi power log. Label: offline VSR encode power, not live playback."""

from __future__ import annotations

import shutil
import subprocess
import threading
from pathlib import Path


class PowerLogger:
    def __init__(self, csv_path: Path, gpu_name: str = "") -> None:
        self.csv_path = Path(csv_path)
        self.gpu_name = gpu_name
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._smi = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
        if not Path(self._smi).is_file():
            self._smi = None

    def start(self) -> bool:
        if not self._smi:
            return False
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# offline VSR encode power, not live playback\n"
            f"# gpu: {self.gpu_name}\n"
            "timestamp, power.draw, utilization.gpu\n"
        )
        self.csv_path.write_text(header, encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                [
                    self._smi,
                    "--query-gpu=timestamp,power.draw,utilization.gpu",
                    "--format=csv,noheader,nounits",
                    "-l", "1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError:
            self._proc = None
            return False
        self._thread = threading.Thread(target=self._pump, name="vsr-power", daemon=True)
        self._thread.start()
        return True

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            with self.csv_path.open("a", encoding="utf-8") as f:
                for line in self._proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    f.write(line + "\n")
                    f.flush()
        except OSError:
            pass

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except OSError:
                    pass
        self._proc = None
