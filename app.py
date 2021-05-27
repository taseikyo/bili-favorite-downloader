#!/usr/bin/python3
# -*- coding: utf-8 -*-
# @Date    : 2021-05-12 20:00:29
# @Author  : Lewis Tian (taseikyo@gmail.com)
# @Link    : github.com/taseikyo

import io
import json
import os
import subprocess
import sys
import time
from collections import namedtuple
from urllib.request import urlopen

from PyQt6.QtCore import QDir, QMutex, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QCursor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

WorkerRespnose = namedtuple(
    "WorkerRespnose", "thumb_img title author medias media_counts publish_date"
)

DownloadCountsMutex = QMutex()


# seperate worker thread for background processing and to avoid UI freez
class WorkerThread(QThread):
    # setup response signal
    worker_response = pyqtSignal(WorkerRespnose)
    # setup error signal
    worker_err_response = pyqtSignal()
    # additional parameter as url

    def __init__(self, media_id):
        # invoke the __init__ of super as well
        super(WorkerThread, self).__init__()
        self.media_id = media_id

    def run(self):
        url = (
            f"https://api.bilibili.com/medialist/gateway/base/spaceDetail?media_id={self.media_id}&"
            "pn=1&ps=20&keyword=&order=mtime&type=0&tid=0&jsonp=jsonp"
        )
        try:
            data = json.loads(urlopen(url).read())
            # load thumbnail image
            pixmap = QPixmap()
            pixmap.loadFromData(urlopen(str(data["data"]["info"]["cover"])).read())
            # emitting the response signal
            #
            self.worker_response.emit(
                WorkerRespnose(
                    pixmap,
                    data["data"]["info"]["title"],
                    data["data"]["info"]["upper"]["name"],
                    data["data"]["medias"],
                    data["data"]["info"]["media_count"],
                    data["data"]["info"]["ctime"],
                )
            )
        except Exception as e:
            print(e)
            # emitting the error signal
            self.worker_err_response.emit()


class _BufferedReaderForFFmpeg(io.BufferedReader):
    """Method `newline` overriden to *also* treat `\\r` as a line break."""

    def readline(self, size=-1):
        if hasattr(self, "peek"):

            def nreadahead():
                readahead = self.peek(1)
                if not readahead:
                    return 1
                n = (
                    (readahead.find(b"\r") + 1)
                    or (readahead.find(b"\n") + 1)
                    or len(readahead)
                )
                if size >= 0:
                    n = min(n, size)
                return n

        else:

            def nreadahead():
                return 1

        if size is None:
            size = -1
        else:
            try:
                size_index = size.__index__
            except AttributeError:
                raise TypeError(f"{size!r} is not an integer")
            else:
                size = size_index()
        res = bytearray()
        while size < 0 or len(res) < size:
            b = self.read(nreadahead())
            if not b:
                break
            res += b
            if os.linesep == "\r\n":
                # Windows
                if res.endswith(b"\r"):
                    if self.peek(1).startswith(b"\n"):
                        # \r\n encountered
                        res += self.read(1)
                    break
            else:
                # Unix
                if res.endswith(b"\r") or res.endswith(b"\n"):
                    break
        return bytes(res)


# download thread
class DownloadThread(QThread):
    # setup download respomse signal
    download_response = pyqtSignal(int)
    # setup download complete signal
    download_complete = pyqtSignal(str)
    # setup download error signal
    download_err = pyqtSignal()

    def __init__(self, media_id, media_counts, first_page_medias, output_path):
        super(DownloadThread, self).__init__()
        self.media_id = media_id
        self.media_counts = media_counts
        self.page_medias = first_page_medias
        self.output_path = output_path

        # set multithreads to False
        self.is_multithreads = True
        self.threads = {}

    def run(self):
        if self.is_multithreads:
            self.multi()
        else:
            self.single()

    def single(self):
        try:
            counts = 0
            page = 2
            while True:
                # download video using annie
                for media in self.page_medias:
                    per_counts = 0
                    cmd = f"annie -o {self.output_path} https://www.bilibili.com/video/{media['bvid']}"
                    print(cmd)
                    # os.system(cmd)
                    self.process = subprocess.Popen(
                        cmd,
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    )
                    stdout = _BufferedReaderForFFmpeg(self.process.stdout.raw)
                    while True:
                        line = stdout.readline()
                        if not line:
                            break
                        try:
                            line = line.decode("utf-8")
                        except UnicodeDecodeError:
                            line = line.decode("gbk")
                        print(line)
                        try:
                            per_counts = float(line.split("%")[0].split(" ")[-1])
                        except:
                            pass
                        self.download_response.emit(
                            int((counts + per_counts / 100) / self.media_counts * 100)
                        )
                    counts += 1

                if counts >= self.media_counts:
                    break
                url = (
                    f"https://api.bilibili.com/medialist/gateway/base/spaceDetail?media_id={self.media_id}&"
                    f"pn={page}&ps=20&keyword=&order=mtime&type=0&tid=0&jsonp=jsonp"
                )
                page += 1
                data = json.loads(urlopen(url).read())
                self.page_medias = data["data"]["medias"]
        except Exception as e:
            print(e)
            # emitting the error signal
            self.download_err.emit()

        self.download_complete.emit(self.output_path)

    def multi(self):
        """
        多线程下载就不显示细节进度条了
        同步太麻烦了
        """
        # videos counts
        self.download_counts = 0
        # progress counts
        self.cur_progress = 0

        try:
            counts = 0
            page = 2
            while True:
                # download video using annie
                for media in self.page_medias:
                    bvid = media["bvid"]
                    self.threads[bvid] = Annie(bvid, self.output_path)
                    self.threads[bvid].annie_download_complete.connect(
                        self.download_finished_slot
                    )
                    self.threads[bvid].annie_download_err.connect(
                        self.download_err.emit
                    )
                    self.threads[bvid].start()
                    counts += 1

                if counts >= self.media_counts:
                    break
                url = (
                    f"https://api.bilibili.com/medialist/gateway/base/spaceDetail?media_id={self.media_id}&"
                    f"pn={page}&ps=20&keyword=&order=mtime&type=0&tid=0&jsonp=jsonp"
                )
                page += 1
                data = json.loads(urlopen(url).read())
                self.page_medias = data["data"]["medias"]
        except Exception as e:
            print(e)
            # emitting the error signal
            self.download_err.emit()

        while self.download_counts != self.media_counts:
            pass
        self.download_complete.emit(self.output_path)

    def download_finished_slot(self, bvid):
        # Remove threads that have completed their tasks
        del self.threads[bvid]
        # lock
        DownloadCountsMutex.lock()
        self.download_counts += 1
        DownloadCountsMutex.unlock()

        self.download_response.emit(int(self.download_counts / self.media_counts * 100))

    def terminate(self):
        super(DownloadThread, self).terminate()

        if self.is_multithreads:
            for t in self.threads.values():
                subprocess.call(f"TASKKILL /F /PID {t.process.pid} /T")
        else:
            subprocess.call(f"TASKKILL /F /PID {self.process.pid} /T")


# download video using annie (for multithreads)
class Annie(QThread):
    annie_download_complete = pyqtSignal(str)
    annie_download_err = pyqtSignal()

    def __init__(self, bvid, output_path):
        super(Annie, self).__init__()
        self.bvid = bvid
        self.output_path = output_path
        self.process = None

        if not os.path.exists(output_path):
            os.mkdir(output_path)

    def run(self):
        cmd = f"annie -o {self.output_path} https://www.bilibili.com/video/{self.bvid}"
        print(cmd)
        try:
            self.process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout = _BufferedReaderForFFmpeg(self.process.stdout.raw)
            while True:
                line = stdout.readline()
                if not line:
                    break
        except:
            self.annie_download_err.emit()

        self.annie_download_complete.emit(self.bvid)


class B23Download(QWidget):
    def __init__(self):
        super(B23Download, self).__init__()
        # setup some flags
        self.is_fetching = False
        self.is_downloading = False

        # default output path
        basepath = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(basepath, "videos")
        self.output_path = path

        # setup some window specific things
        self.setWindowTitle("Bilibili Favorite Downloader")
        self.setWindowIcon(QIcon("images/icon_bilibili.ico"))
        self.setFixedSize(705, 343)

        # parent layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(15, 15, 15, 10)
        self.setLayout(main_layout)

        # top bar layout
        top_layout = QHBoxLayout()

        # detail section
        mid_main_layout = QHBoxLayout()
        mid_right_layout = QVBoxLayout()

        # download section
        bottom_main_layout = QHBoxLayout()
        bottom_right_layout = QVBoxLayout()

        # output path link button
        self.output_btn = QPushButton("📂  Output Path")
        self.output_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.output_btn.setToolTip(self.output_path)
        self.output_btn.clicked.connect(self.set_output_path)

        # status bar
        self.status_bar = QStatusBar()

        # message box
        self.message_box = QMessageBox()

        # setting up widgets
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("🔍 Enter or paste favorite URL...")
        self.get_btn = QPushButton("Get")
        self.get_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.get_btn.clicked.connect(self.get_details)

        # thumbnail
        pixmap = QPixmap("images/placeholder.png")
        self.thumb = QLabel()
        self.thumb.setFixedSize(250, 141)
        self.thumb.setScaledContents(True)
        self.thumb.setPixmap(pixmap)

        # detail widgets
        self.title = QLabel("Title: ")
        self.author = QLabel("Author: ")
        self.length = QLabel("Videos: ")
        self.publish_date = QLabel("Published: ")

        # progress bar
        self.progress_bar = QProgressBar()

        # download options
        self.download_btn = QPushButton(" Download Videos ")
        self.download_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.download_btn.clicked.connect(self.get_content)
        self.download_btn.setEnabled(False)
        self.download_btn.setShortcut("Ctrl+Return")
        self.download_btn.setMinimumWidth(200)

        # add widgets and layouts
        top_layout.addWidget(self.url_edit)
        top_layout.addWidget(self.get_btn)

        # detail section
        mid_right_layout.addWidget(self.title)
        mid_right_layout.addWidget(self.author)
        mid_right_layout.addWidget(self.length)
        mid_right_layout.addWidget(self.publish_date)
        mid_main_layout.addWidget(self.thumb)
        mid_main_layout.addSpacing(20)
        mid_main_layout.addLayout(mid_right_layout)

        # download section
        bottom_right_layout.addWidget(self.download_btn)
        bottom_main_layout.addWidget(self.progress_bar)
        bottom_main_layout.addSpacing(10)
        bottom_main_layout.addLayout(bottom_right_layout)

        # status bar
        self.status_bar.setSizeGripEnabled(False)
        self.status_bar.addPermanentWidget(self.output_btn)

        # add content to parent layout
        main_layout.addLayout(top_layout)
        main_layout.addSpacing(20)
        main_layout.addLayout(mid_main_layout)
        main_layout.addSpacing(5)
        main_layout.addLayout(bottom_main_layout)
        main_layout.addWidget(self.status_bar)

    # set output path slot
    def set_output_path(self):
        # update the output path
        path = str(QFileDialog.getExistingDirectory(self, "Select Output Directory"))
        if path:
            self.output_path = path
            # update tooltip
            self.output_btn.setToolTip(path)

    # get button slot
    def get_details(self):
        text = self.url_edit.text().strip()

        if not text:
            return

        if text.find("fid") < 0:
            self.message_box.warning(
                self,
                "Error",
                (
                    "Input a correct favorite URL!\n"
                    "For example: https://space.bilibili.com/xxx/favlist?fid=xxx..."
                ),
            )
            return

        if self.get_btn.text() == "Get":
            self.get_btn.setText("Stop")
            # indicate progress bar as busy
            self.progress_bar.setRange(0, 0)
            # set fetching flag
            self.is_fetching = True
            # setup a worker thread to keep UI responsive
            self.media_id = text.split("fid=")[-1].split("&")[0]
            self.worker = WorkerThread(self.media_id)
            self.worker.start()
            # catch the finished signal
            self.worker.finished.connect(self.finished_slot)
            # catch the response signal
            self.worker.worker_response.connect(self.response_slot)
            # catch the error signal
            self.worker.worker_err_response.connect(self.err_slot)
        elif self.get_btn.text() == "Stop":
            if self.is_fetching:
                # stop worker thread
                self.worker.terminate()
                # set back the get_btn text
                self.get_btn.setText("Get")
            elif self.is_downloading:
                # stop download thread
                self.download_thread.terminate()
                # show the warning message_box
                self.message_box.information(
                    self,
                    "Interrupted",
                    "Download interrupted!\nThe process was aborted while the file was being downloaded... ",
                )
                # reset progress bar
                self.progress_bar.reset()

    # download options slot
    def get_content(self):
        if self.is_fetching:
            # show the warning message
            self.message_box.critical(
                self,
                "Error",
                "Please wait!\nWait while the details are being fetched... ",
            )
        else:
            # disable the download options
            self.download_btn.setDisabled(True)
            # set downloading flag
            self.is_downloading = True
            # set button to stop
            self.get_btn.setText("Stop")
            self.download_thread = DownloadThread(
                self.media_id,
                self.media_counts,
                self.first_page_medias,
                self.output_path,
            )
            # start the thread
            self.download_thread.start()
            # catch the finished signal
            self.download_thread.finished.connect(self.download_finished_slot)
            # catch the response signal
            self.download_thread.download_response.connect(self.download_response_slot)
            # catch the complete signal
            self.download_thread.download_complete.connect(self.download_complete_slot)
            # catch the error signal
            self.download_thread.download_err.connect(self.download_err_slot)

    # handling enter key for get/stop button
    def keyPressEvent(self, event):
        self.url_edit.setFocus()
        if (
            event.key() == Qt.Key.Key_Enter.value
            or event.key() == Qt.Key.Key_Return.value
        ):
            self.get_details()

    # finished slot
    def finished_slot(self):
        # remove progress bar busy indication
        self.progress_bar.setRange(0, 100)
        # unset fetching flag
        self.is_fetching = False

    # response slot
    def response_slot(self, res):
        # set back the button text
        self.get_btn.setText("Get")
        # set the actual thumbnail of requested video
        self.thumb.setPixmap(res.thumb_img)
        # slice the title if it is more than the limit
        if len(res.title) > 50:
            self.title.setText(f"Title: {res.title[:50]}...")
        else:
            self.title.setText(f"Title: {res.title}")
        # cache first page medias
        self.first_page_medias = res.medias
        self.media_counts = res.media_counts
        # set leftover details
        self.author.setText(f"Author: {res.author}")
        self.length.setText(f"Videos: {res.media_counts}")
        self.publish_date.setText(
            f'Published: {time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(res.publish_date))}'
        )
        self.download_btn.setDisabled(False)

    # error slot
    def err_slot(self):
        # show the warning message
        self.message_box.warning(
            self,
            "Warning",
            "Something went wrong!\nProbably a broken link or some restricted content... ",
        )
        # set back the button text
        self.get_btn.setText("Get")

    # download finished slot
    def download_finished_slot(self):
        # set back the button text
        self.get_btn.setText("Get")
        # now enable the download options
        self.download_btn.setDisabled(False)
        # unset downloading flag
        self.is_downloading = False
        # reset pogress bar
        self.progress_bar.reset()

    # download response slot
    def download_response_slot(self, per):
        # update progress bar
        self.progress_bar.setValue(per)
        # adjust the font color to maintain the contrast
        if per > 52:
            self.progress_bar.setStyleSheet("QProgressBar { color: #fff }")
        else:
            self.progress_bar.setStyleSheet("QProgressBar { color: #000 }")

    # download complete slot
    def download_complete_slot(self, location):
        # use native separators
        location = QDir.toNativeSeparators(location)
        # show the success message
        if (
            self.message_box.information(
                self,
                "Downloaded",
                f"Download complete!\nFile was successfully downloaded to :\n{location}\n\nOpen the downloaded file now ?",
                QMessageBox.StandardButtons.Open,
                QMessageBox.StandardButtons.Cancel,
            )
            is QMessageBox.StandardButtons.Open
        ):
            subprocess.Popen(f"explorer /select,{location}")

    # download error slot
    def download_err_slot(self):
        # show the error message
        self.message_box.critical(
            self,
            "Error",
            "Error!\nSomething unusual happened and was unable to download...",
        )


if __name__ == "__main__":
    # instantiate the application
    app = QApplication(sys.argv)
    # setup a custom styleSheet
    with open(f"{os.path.dirname(os.path.abspath(__file__))}/app.css") as f:
        app.setStyleSheet(f.read())
    window = B23Download()
    # show the window at last
    window.show()
    sys.exit(app.exec())
