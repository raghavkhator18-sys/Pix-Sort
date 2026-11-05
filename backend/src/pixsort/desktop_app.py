"""Minimal local PixSort desktop application.

Run with ``python -m pixsort.desktop_app`` after installing the desktop extra.
"""

from pathlib import Path
from threading import Event

from .config import AnalysisConfig
from .pipeline import AnalysisCancelled, AnalysisReport, analyze_directory


def main() -> int:
    try:
        from PySide6.QtCore import QObject, QThread, Signal, Slot
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QLabel,
            QMainWindow,
            QMessageBox,
            QPushButton,
            QProgressBar,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise SystemExit(
            "PySide6 is required for the desktop app. Install with: "
            "pip install -e .[desktop]"
        ) from exc

    class AnalysisWorker(QObject):
        progress = Signal(int, int, str)
        completed = Signal(object, str)
        failed = Signal(str)
        cancelled = Signal()

        def __init__(self, folder: Path, output: Path) -> None:
            super().__init__()
            self.folder = folder
            self.output = output
            self.cancel_event = Event()

        @Slot()
        def run(self) -> None:
            try:
                report = analyze_directory(
                    self.folder,
                    AnalysisConfig(),
                    cancel_event=self.cancel_event,
                    progress_callback=self.progress.emit,
                )
                if self.cancel_event.is_set():
                    raise AnalysisCancelled("Analysis cancelled by user.")
                report.write_json(self.output / "pixsort-report.json")
                report.write_html(self.output / "pixsort-report.html")
                self.completed.emit(report, str(self.output))
            except AnalysisCancelled:
                self.cancelled.emit()
            except (OSError, ValueError, RuntimeError) as exc:
                self.failed.emit(str(exc))

        def cancel(self) -> None:
            self.cancel_event.set()

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("PixSort - Local Photo Review")
            self.resize(520, 260)
            self.folder: Path | None = None
            self.thread: QThread | None = None
            self.worker: AnalysisWorker | None = None

            self.folder_label = QLabel("No photo folder selected.")
            self.status_label = QLabel("Choose a local folder to begin.")
            self.progress = QProgressBar()
            self.progress.setRange(0, 1)
            self.choose_button = QPushButton("Choose photo folder")
            self.analyze_button = QPushButton("Analyze")
            self.cancel_button = QPushButton("Cancel")
            self.cancel_button.setEnabled(False)
            self.analyze_button.setEnabled(False)
            self.choose_button.clicked.connect(self.choose_folder)
            self.analyze_button.clicked.connect(self.start_analysis)
            self.cancel_button.clicked.connect(self.cancel_analysis)

            layout = QVBoxLayout()
            layout.addWidget(self.folder_label)
            layout.addWidget(self.choose_button)
            layout.addWidget(self.analyze_button)
            layout.addWidget(self.cancel_button)
            layout.addWidget(self.progress)
            layout.addWidget(self.status_label)
            container = QWidget()
            container.setLayout(layout)
            self.setCentralWidget(container)

        @Slot()
        def choose_folder(self) -> None:
            selected = QFileDialog.getExistingDirectory(self, "Choose photo folder")
            if selected:
                self.folder = Path(selected)
                self.folder_label.setText(str(self.folder))
                self.status_label.setText("Ready to analyze locally.")
                self.analyze_button.setEnabled(True)

        @Slot()
        def start_analysis(self) -> None:
            if self.folder is None or not self.folder.is_dir():
                QMessageBox.warning(self, "Invalid folder", "Choose an existing photo folder.")
                return
            output = Path.cwd() / "output" / "desktop" / self.folder.name
            self.thread = QThread(self)
            self.worker = AnalysisWorker(self.folder, output)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.progress.connect(self.update_progress)
            self.worker.completed.connect(self.analysis_completed)
            self.worker.failed.connect(self.analysis_failed)
            self.worker.cancelled.connect(self.analysis_cancelled)
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.start()
            self.choose_button.setEnabled(False)
            self.analyze_button.setEnabled(False)
            self.cancel_button.setEnabled(True)
            self.status_label.setText("Analyzing locally; originals are read-only.")

        @Slot(int, int, str)
        def update_progress(self, done: int, total: int, message: str) -> None:
            self.progress.setRange(0, max(total, 1))
            self.progress.setValue(done)
            self.status_label.setText(message)

        def finish_worker(self) -> None:
            self.choose_button.setEnabled(True)
            self.analyze_button.setEnabled(self.folder is not None)
            self.cancel_button.setEnabled(False)
            if self.thread:
                self.thread.quit()
                self.thread.wait()
            self.worker = None
            self.thread = None

        @Slot(object, str)
        def analysis_completed(self, report: AnalysisReport, output: str) -> None:
            self.finish_worker()
            self.status_label.setText(
                f"Done: {report.summary['processed']} processed, "
                f"{len(report.recommendations)} review items."
            )
            QMessageBox.information(
                self,
                "Analysis complete",
                f"Reports saved locally in:\n{output}",
            )

        @Slot(str)
        def analysis_failed(self, message: str) -> None:
            self.finish_worker()
            self.status_label.setText("Analysis failed.")
            QMessageBox.critical(self, "Analysis error", message)

        @Slot()
        def analysis_cancelled(self) -> None:
            self.finish_worker()
            self.status_label.setText("Analysis cancelled; no originals were changed.")

        @Slot()
        def cancel_analysis(self) -> None:
            if self.worker:
                self.worker.cancel()
                self.status_label.setText("Cancelling...")

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
