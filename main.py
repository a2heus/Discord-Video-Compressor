import os
import sys
import math
import subprocess
import re
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
import imageio_ffmpeg

APP_NAME = "Discord Video Compressor"
DEFAULT_SIZES_MIB = [10, 50, 500]

# audio shit
def audio_bitrate_for_target(mib: int) -> int:
    if mib <= 10:
        return 64_000
    if mib <= 50:
        return 96_000
    return 128_000

# ffprobe else none
def find_ffprobe() -> str | None:
    if shutil_which := shutil.which("ffprobe"):
        return shutil_which
    try:
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        cand = Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if cand.exists():
            return str(cand)
    except Exception:
        pass
    return None

def probe_duration_seconds(path: str, ffmpeg_exe: str, ffprobe_exe: str | None) -> float:
    if ffprobe_exe:
        try:
            p = subprocess.run([ffprobe_exe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", path],
                               capture_output=True, text=True)
            if p.returncode == 0:
                s = p.stdout.strip().replace(",", ".")
                return float(s)
        except Exception:
            pass
    try:
        p = subprocess.run([ffmpeg_exe, "-i", path], capture_output=True, text=True)
        err = p.stderr or p.stdout
        m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", err)
        if m:
            hh = int(m.group(1)); mm = int(m.group(2)); ss = float(m.group(3))
            return hh*3600 + mm*60 + ss
    except Exception:
        pass
    return 0.0

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")

class EncodeWorker(QtCore.QObject):
    progress = QtCore.Signal(str, int)   # message, percent
    finished = QtCore.Signal(bool, str)  # ok, last_output

    def __init__(self, inputs: list[str], outdir: str, target_mib: int,
                 two_pass: bool, auto_tune: bool, parent=None) -> None:
        super().__init__(parent)
        self.inputs = inputs
        self.outdir = Path(outdir)
        self.target_mib = target_mib
        self.two_pass = two_pass
        self.auto_tune = auto_tune
        self._abort = False
        self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.ffprobe_exe = find_ffprobe()

    @QtCore.Slot()
    def run(self):
        self.outdir.mkdir(parents=True, exist_ok=True)
        ok = True
        last_out = ""
        for src in self.inputs:
            if self._abort:
                ok = False
                break
            name = Path(src).name
            self.progress.emit(f"Analyzing {name}", 0)
            dur = probe_duration_seconds(src, self.ffmpeg_exe, self.ffprobe_exe)
            if dur <= 0:
                self.progress.emit(f"Cannot read duration for {name}", 0)
                ok = False
                break

            target_bytes = self.target_mib * 1024 * 1024
            total_bps = (target_bytes * 8.0) / max(0.1, dur)
            a_bps = audio_bitrate_for_target(self.target_mib)
            v_bps = max(120_000, int(total_bps - a_bps))

            outpath = self.outdir / f"{Path(src).stem}_{self.target_mib}MiB.mp4"
            last_out = str(outpath)

            ok = self.encode_one(src, str(outpath), dur, v_bps, a_bps)
            if not ok:
                break

            if self.auto_tune:
                try:
                    size = outpath.stat().st_size
                except Exception:
                    size = 0
                if size <= 0:
                    ok = False
                    break
                ratio = size / target_bytes
                if ratio > 1.06 or ratio < 0.85:
                    new_v_bps = max(100_000, int(v_bps / ratio))
                    self.progress.emit(f"Auto-tune bitrate → {new_v_bps//1000} kbps", 0)
                    ok = self.encode_one(src, str(outpath), dur, new_v_bps, a_bps)
                    if not ok:
                        break

        self.finished.emit(ok, last_out)

    def abort(self):
        self._abort = True

    def encode_one(self, inp: str, outp: str, dur: float, v_bps: int, a_bps: int) -> bool:
        for f in Path.cwd().glob("ffmpeg2pass-*.*"):
            try: f.unlink()
            except: pass

        if self.two_pass:
            if not self._ffmpeg_pass(inp, outp, v_bps, a_bps, passnum=1, dur=dur):
                return False
            if not self._ffmpeg_pass(inp, outp, v_bps, a_bps, passnum=2, dur=dur):
                return False
        else:
            if not self._ffmpeg_pass(inp, outp, v_bps, a_bps, passnum=0, dur=dur):
                return False
        return True

    def _ffmpeg_pass(self, inp: str, outp: str, v_bps: int, a_bps: int, passnum: int, dur: float) -> bool:
        cmd = [self.ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "info",
               "-i", inp,
               "-c:v", "libx264", "-b:v", str(v_bps),
               "-maxrate", str(int(v_bps * 1.1)),
               "-bufsize", str(max(100000, int(v_bps * 2))),
               "-preset", "medium",
               "-pix_fmt", "yuv420p"]
        passlog = f"ffmpeg2pass-{Path(inp).stem}"
        if passnum == 1:
            cmd += ["-an", "-pass", "1", "-passlogfile", passlog, "-f", "mp4", os.devnull]
        elif passnum == 2:
            cmd += ["-c:a", "aac", "-b:a", str(a_bps), "-pass", "2", "-passlogfile", passlog, "-movflags", "+faststart", outp]
        else:
            cmd += ["-c:a", "aac", "-b:a", str(a_bps), "-movflags", "+faststart", outp]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, universal_newlines=True)
        except FileNotFoundError:
            self.progress.emit("ffmpeg not found (imageio-ffmpeg should supply it)", 0)
            return False

        last_pct = -1
        for line in proc.stderr:
            m = TIME_RE.search(line)
            if m:
                hh = int(m.group(1)); mm = int(m.group(2)); ss = float(m.group(3))
                t = hh*3600 + mm*60 + ss
                pct = max(0, min(100, int((t / max(0.1, dur)) * 100)))
                if pct != last_pct:
                    self.progress.emit(f"{Path(inp).name}: {pct}%", pct)
                    last_pct = pct
        proc.wait()
        if proc.returncode != 0:
            self.progress.emit(f"ffmpeg failed (code {proc.returncode})", last_pct if last_pct>=0 else 0)
            return False
        self.progress.emit(f"Done {Path(inp).name}", 100)
        return True


class DropList(QtWidgets.QListWidget):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setStyleSheet("QListWidget { background:#20232a; color:#e5e5e5; border:1px solid #3a3f47; }")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                p = url.toLocalFile()
                if p and os.path.isfile(p):
                    if not any(self.item(i).text() == p for i in range(self.count())):
                        self.addItem(p)
            e.acceptProposedAction()
        else:
            super().dropEvent(e)


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(980, 560)
        self.setStyleSheet("""
            QWidget { background:#181a1f; color:#e5e5e5; font-family:Segoe UI,Arial; }
            QLineEdit, QComboBox, QPushButton { background:#262a33; border:1px solid #3a3f47; padding:6px; border-radius:6px; }
            QPushButton:hover { border-color:#5865f2; }
            QProgressBar { border:1px solid #3a3f47; border-radius:6px; text-align:center; }
            QProgressBar::chunk { background-color:#5865f2; }
            QGroupBox { border:1px solid #2b2f36; margin-top:12px; border-radius:8px; }
            QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#aab; }
        """)

        self.list = DropList()
        self.addBtn = QtWidgets.QPushButton("Add files…")
        self.remBtn = QtWidgets.QPushButton("Remove selected")
        self.clearBtn = QtWidgets.QPushButton("Clear")
        self.outBtn = QtWidgets.QPushButton("Open output folder")

        self.outputEdit = QtWidgets.QLineEdit(str(Path.cwd() / "Output"))
        self.outputBrowse = QtWidgets.QPushButton("⋯")

        self.sizeCombo = QtWidgets.QComboBox()
        self.sizeCombo.addItems(["10 MiB", "50 MiB", "500 MiB", "Custom…"])
        self.twoPass = QtWidgets.QCheckBox("Use 2-pass (more accurate)")
        self.twoPass.setChecked(True)
        self.autoTune = QtWidgets.QCheckBox("Auto-tune if off-size (retry)")
        self.autoTune.setChecked(True)

        self.goBtn = QtWidgets.QPushButton("Compress")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QtWidgets.QLabel("Ready")

        # Layout left
        leftCol = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel("Discord Video Compressor")
        title.setStyleSheet("QLabel{font-size:22px; font-weight:700; color:#fff}")
        leftCol.addWidget(title)
        leftCol.addSpacing(8)
        leftCol.addWidget(self.list, 1)
        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.addBtn)
        h.addWidget(self.remBtn)
        h.addWidget(self.clearBtn)
        leftCol.addLayout(h)

        # Layout right
        rightCol = QtWidgets.QVBoxLayout()
        gbOut = QtWidgets.QGroupBox("Output")
        g1 = QtWidgets.QGridLayout(gbOut)
        g1.addWidget(QtWidgets.QLabel("Folder"), 0, 0)
        g1.addWidget(self.outputEdit, 0, 1)
        g1.addWidget(self.outputBrowse, 0, 2)
        g1.addWidget(self.outBtn, 1, 1)

        gbEnc = QtWidgets.QGroupBox("Encoding")
        g2 = QtWidgets.QGridLayout(gbEnc)
        g2.addWidget(QtWidgets.QLabel("Target size"), 0, 0)
        g2.addWidget(self.sizeCombo, 0, 1)
        g2.addWidget(self.twoPass, 1, 1)
        g2.addWidget(self.autoTune, 2, 1)

        rightCol.addWidget(gbOut)
        rightCol.addWidget(gbEnc)
        rightCol.addStretch(1)
        rightCol.addWidget(self.goBtn)
        rightCol.addWidget(self.progress)
        rightCol.addWidget(self.status)

        root = QtWidgets.QHBoxLayout(self)
        root.addLayout(leftCol, 2)
        root.addLayout(rightCol, 1)

        # Signals
        self.addBtn.clicked.connect(self.on_add)
        self.remBtn.clicked.connect(self.on_remove)
        self.clearBtn.clicked.connect(lambda: self.list.clear())
        self.outBtn.clicked.connect(self.on_open_out)
        self.outputBrowse.clicked.connect(self.on_browse_out)
        self.goBtn.clicked.connect(self.on_go)

        self.thread: QtCore.QThread | None = None
        self.worker: EncodeWorker | None = None

    def target_mib(self) -> int:
        t = self.sizeCombo.currentText()
        if t.startswith("10"): return 10
        if t.startswith("50"): return 50
        if t.startswith("500"): return 500
        mib, ok = QtWidgets.QInputDialog.getInt(self, APP_NAME, "Custom MiB:", 25, 1, 100000, 1)
        return int(mib) if ok else 0

    def on_add(self):
        dlg = QtWidgets.QFileDialog(self)
        dlg.setFileMode(QtWidgets.QFileDialog.ExistingFiles)
        dlg.setNameFilter("Videos (*.mp4 *.mov *.mkv *.avi *.webm *.ts *.m4v);;All files (*.*)")
        if dlg.exec():
            for p in dlg.selectedFiles():
                if not any(self.list.item(i).text() == p for i in range(self.list.count())):
                    self.list.addItem(p)

    def on_remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def on_open_out(self):
        p = Path(self.outputEdit.text()).resolve()
        p.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.call(["open", str(p)])
        else:
            subprocess.call(["xdg-open", str(p)])

    def on_browse_out(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose output folder", self.outputEdit.text())
        if d:
            self.outputEdit.setText(d)

    def on_go(self):
        files = [self.list.item(i).text() for i in range(self.list.count())]
        if not files:
            QtWidgets.QMessageBox.warning(self, APP_NAME, "Add at least one video.")
            return
        target = self.target_mib()
        if target <= 0:
            return
        outdir = self.outputEdit.text().strip() or str(Path.cwd() / "Output")

        self.progress.setValue(0)
        self.status.setText("Starting…")

        self.thread = QtCore.QThread(self)
        self.worker = EncodeWorker(files, outdir, target, two_pass=True, auto_tune=True)
        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished.connect(self.on_finished)
        self.thread.started.connect(self.worker.run)
        self.thread.start()
        self.setEnabled(False)

    @QtCore.Slot(str, int)
    def on_progress(self, msg: str, pct: int):
        self.status.setText(msg)
        self.progress.setValue(int(pct))

    @QtCore.Slot(bool, str)
    def on_finished(self, ok: bool, last_out: str):
        self.setEnabled(True)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None
        if ok:
            self.status.setText("All tasks finished.")
            QtWidgets.QMessageBox.information(self, APP_NAME, "All tasks finished.")
        else:
            self.status.setText("Error. See status.")
            QtWidgets.QMessageBox.critical(self, APP_NAME, "Encoding failed.")


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # dépendance
    import shutil
    main()
