"""Tray interface for the automatic ScreenWatcher extension."""
import ctypes
import multiprocessing as mp
import sys
import traceback

from PySide6.QtCore import QSettings, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget
from AutomaticWatch import Monitor, ROOT, OUTPUT

TEXT = {
 'zh': {'title':'任务管理器自动取证', 'folder':'打开 D 盘记录', 'quit':'退出', 'show':'查看状态',
 'pause':'暂停', 'resume':'继续', 'hint':'持续异常 30 秒后保存两份转储，验证成功再重启任务管理器、继续检测。',
 'starting':'正在启动', 'waiting':'等待任务管理器 CPU → 逻辑处理器页面', 'normal':'总图正常变化',
 'candidate':'总图水平，正在确认', 'no_motion':'等待分线程曲线变化', 'suspect':'疑似汇总冻结，已触发取证',
 'invalid':'当前曲线无法识别', 'capture_error':'无法读取图表', 'capture_timeout':'读取超时，正在恢复',
 'error':'检测错误', 'stopped':'已暂停', 'cooldown':'取证冷却中', 'storage_limit':'已达存储限额，暂停转储',
 'system':'跟随系统', 'light':'浅色', 'dark':'深色', 'complete':'转储完成', 'dump_failed':'转储失败，请查看记录',
 'capturing':'正在写入转储', 'verification_complete':'安装验证转储完成',
 'recovered':'取证完成，任务管理器已重启', 'recovery_failed':'取证已保存，自动恢复未完成'},
 'en': {'title':'Task Manager automatic capture', 'folder':'Open records on D:', 'quit':'Quit', 'show':'Status',
 'pause':'Pause', 'resume':'Resume', 'hint':'After 30 seconds of suspected freezing, saves and verifies two dumps, then restarts Task Manager and resumes detection.',
 'starting':'Starting', 'waiting':'Waiting for Task Manager CPU / logical processors page', 'normal':'Aggregate changing',
 'candidate':'Aggregate flat; confirming', 'no_motion':'Waiting for logical graph movement', 'suspect':'Suspected freeze; capture triggered',
 'invalid':'Graph not recognized', 'capture_error':'Cannot read charts', 'capture_timeout':'Capture timed out; recovering',
 'error':'Detection error', 'stopped':'Paused', 'cooldown':'Capture cooldown', 'storage_limit':'Storage limit; dumps paused',
 'system':'System', 'light':'Light', 'dark':'Dark', 'complete':'Dumps complete', 'dump_failed':'Dump failed; see records',
 'capturing':'Writing dumps', 'verification_complete':'Installation dump verification complete',
 'recovered':'Evidence saved; Task Manager restarted', 'recovery_failed':'Evidence saved; recovery incomplete'}
}

class Watch(QWidget):
    def __init__(self):
        super().__init__()
        self.settings=QSettings(str(ROOT/'.local/preferences.ini'),QSettings.IniFormat)
        self.lang=self.settings.value('language','zh')
        self.monitor=Monitor()
        self.setWindowIcon(QIcon(str(ROOT/'assets/icon.png')))
        layout=QVBoxLayout(self)
        row=QHBoxLayout()
        self.language=QComboBox();self.language.addItems(['简体中文','English'])
        self.language.setCurrentIndex(0 if self.lang=='zh' else 1)
        self.theme=QComboBox();self.theme.addItems(['System','Light','Dark'])
        self.theme.setCurrentIndex(int(self.settings.value('theme',0)))
        row.addStretch();row.addWidget(self.language);row.addWidget(self.theme);layout.addLayout(row)
        self.status=QLabel();self.status.setWordWrap(True);layout.addWidget(self.status)
        self.details=QLabel();self.details.setWordWrap(True);layout.addWidget(self.details)
        self.note=QLabel();self.note.setWordWrap(True);layout.addWidget(self.note)
        row=QHBoxLayout();self.pause=QPushButton();self.folder=QPushButton()
        row.addWidget(self.pause);row.addWidget(self.folder);layout.addLayout(row)
        self.pause.clicked.connect(self.toggle)
        self.folder.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTPUT))))
        self.language.currentIndexChanged.connect(self.translate)
        self.theme.currentIndexChanged.connect(self.apply_theme)
        QApplication.styleHints().colorSchemeChanged.connect(self.apply_theme)
        self.tray=QSystemTrayIcon(self.windowIcon(),self)
        self.menu=QMenu();self.show_action=QAction(self);self.quit_action=QAction(self)
        self.show_action.triggered.connect(self.show);self.quit_action.triggered.connect(self.quit)
        self.menu.addAction(self.show_action);self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(lambda reason:self.show() if reason==QSystemTrayIcon.DoubleClick else None)
        self.tray.show()
        self.translate();self.apply_theme();self.resize(440,210)
        self.timer=QTimer(self);self.timer.setInterval(250);self.timer.timeout.connect(self.tick);self.timer.start()

    def text(self,key):return TEXT[self.lang].get(key,key)
    def translate(self,*_):
        self.lang='zh' if self.language.currentIndex()==0 else 'en'
        self.settings.setValue('language',self.lang)
        self.setWindowTitle(self.text('title'));self.note.setText(self.text('hint'))
        self.folder.setText(self.text('folder'));self.show_action.setText(self.text('show'));self.quit_action.setText(self.text('quit'))
        for i,key in enumerate(('system','light','dark')):self.theme.setItemText(i,self.text(key))
        self.update_status()
    def apply_theme(self,*_):
        choice=self.theme.currentIndex()
        dark=choice==2 or (choice==0 and QApplication.styleHints().colorScheme()==Qt.ColorScheme.Dark)
        bg,fg,panel=('#202124','#f1f3f4','#303134') if dark else ('#f5f6f8','#202124','#ffffff')
        self.setStyleSheet(f'QWidget {{background:{bg};color:{fg};font-size:13px;}} QPushButton,QComboBox {{background:{panel};padding:7px;}}')
        self.settings.setValue('theme',choice)
    def update_status(self):
        state=self.monitor.state
        self.status.setText(self.text(state['state']) + (f" · {state.get('seconds',0):.0f}s" if state.get('seconds') else ''))
        result=self.monitor.capture_result
        self.details.setText(self.text(result) if result else str(OUTPUT))
        self.pause.setText(self.text('pause' if self.monitor.enabled else 'resume'))
        self.tray.setToolTip(self.text('title')+'\n'+self.status.text())
    def tick(self):
        request=ROOT/'.local/stop.request'
        if request.exists():
            request.unlink();self.quit();return
        self.monitor.tick();self.update_status()
    def toggle(self):
        if self.monitor.enabled:self.monitor.stop()
        else:self.monitor.enabled=True
        self.update_status()
    def closeEvent(self,event):self.hide();event.ignore()
    def quit(self):
        self.timer.stop();self.monitor.stop();self.tray.hide();self.settings.sync();QApplication.quit()

def main():
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
    kernel.CreateMutexW.restype=ctypes.c_void_p
    mutex=kernel.CreateMutexW(None,False,'Local\\TaskmgrFlatlineWatch-ABSCOND')
    if not mutex or ctypes.get_last_error()==183:return 0
    (ROOT/'.local/stop.request').unlink(missing_ok=True)
    app=QApplication(sys.argv);app.setQuitOnLastWindowClosed(False);app.setStyle('Fusion')
    widget=Watch()
    if '--show' in sys.argv:widget.show()
    try:return app.exec()
    finally:
        widget.monitor.stop()
        kernel.CloseHandle.argtypes=[ctypes.c_void_p];kernel.CloseHandle(mutex)

if __name__=='__main__':
    mp.freeze_support()
    try:
        sys.exit(main())
    except Exception:
        (ROOT/'.local/startup-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
