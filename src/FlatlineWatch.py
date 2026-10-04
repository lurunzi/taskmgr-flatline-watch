"""Tray interface for the automatic ScreenWatcher extension."""
import ctypes
import multiprocessing as mp
import sys
import traceback

from PySide6.QtCore import QSettings, QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget
from AutomaticWatch import Monitor, ROOT, OUTPUT
import Autostart

TEXT = {
 'zh': {'title':'任务管理器自动取证', 'folder':'打开 D 盘记录', 'quit':'退出', 'show':'查看状态',
 'pause':'暂停', 'resume':'继续', 'hint':'持续异常 30 秒后保存两份转储，验证成功再重启任务管理器、继续检测。',
 'starting':'正在启动', 'waiting':'等待任务管理器 CPU → 逻辑处理器页面', 'normal':'总图正常变化',
 'candidate':'总图水平，正在确认', 'no_motion':'等待分线程曲线变化', 'suspect':'疑似汇总冻结，已触发取证',
 'invalid':'当前曲线无法识别', 'capture_error':'无法读取图表', 'capture_timeout':'读取超时，正在恢复',
 'error':'检测错误', 'stopped':'已暂停', 'cooldown':'取证冷却中', 'storage_limit':'已达存储限额，暂停转储',
 'system':'跟随系统', 'light':'浅色', 'dark':'深色', 'complete':'转储完成', 'dump_failed':'转储失败，请查看记录',
 'capturing':'正在写入转储', 'recovering':'正在重启任务管理器', 'verification_complete':'安装验证转储完成',
 'recovered':'取证完成，任务管理器已重启', 'recovery_failed':'取证已保存，自动恢复未完成',
 'autostart':'开机自启动', 'autostart_missing':'未安装自启动（运行 Install.cmd）', 'query_trace':'查询追踪',
 'restart_only':'只重启，不取证', 'hint_restart':'持续异常 30 秒后直接重启任务管理器，不保存转储和图表，然后继续检测。',
 'restart_cooldown':'重启冷却中', 'restarted':'任务管理器已重启', 'restart_failed':'重启未完成，稍后重试'},
 'en': {'title':'Task Manager automatic capture', 'folder':'Open records on D:', 'quit':'Quit', 'show':'Status',
 'pause':'Pause', 'resume':'Resume', 'hint':'After 30 seconds of suspected freezing, saves and verifies two dumps, then restarts Task Manager and resumes detection.',
 'starting':'Starting', 'waiting':'Waiting for Task Manager CPU / logical processors page', 'normal':'Aggregate changing',
 'candidate':'Aggregate flat; confirming', 'no_motion':'Waiting for logical graph movement', 'suspect':'Suspected freeze; capture triggered',
 'invalid':'Graph not recognized', 'capture_error':'Cannot read charts', 'capture_timeout':'Capture timed out; recovering',
 'error':'Detection error', 'stopped':'Paused', 'cooldown':'Capture cooldown', 'storage_limit':'Storage limit; dumps paused',
 'system':'System', 'light':'Light', 'dark':'Dark', 'complete':'Dumps complete', 'dump_failed':'Dump failed; see records',
 'capturing':'Writing dumps', 'recovering':'Restarting Task Manager', 'verification_complete':'Installation dump verification complete',
 'recovered':'Evidence saved; Task Manager restarted', 'recovery_failed':'Evidence saved; recovery incomplete',
 'autostart':'Start at logon', 'autostart_missing':'Autostart not installed (run Install.cmd)', 'query_trace':'Query trace',
 'restart_only':'Restart only, no evidence', 'hint_restart':'After 30 seconds of suspected freezing, restarts Task Manager directly without saving dumps or graphs, then resumes detection.',
 'restart_cooldown':'Restart cooldown', 'restarted':'Task Manager restarted', 'restart_failed':'Restart incomplete; retrying'}
}

class Watch(QWidget):
    def __init__(self):
        super().__init__()
        self.settings=QSettings(str(ROOT/'.local/preferences.ini'),QSettings.IniFormat)
        self.lang=self.settings.value('language','zh')
        self.monitor=Monitor(query_trace='--query-trace' in sys.argv,
                             restart_only=self.settings.value('restart_only','false')=='true')
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
        row=QHBoxLayout();self.pause=QPushButton();self.folder=QPushButton();self.autostart=QCheckBox();self.restart_only=QCheckBox()
        row.addWidget(self.pause);row.addWidget(self.folder);row.addWidget(self.autostart);row.addWidget(self.restart_only);layout.addLayout(row)
        self.restart_only.clicked.connect(self.set_restart_only)
        self.autostart.clicked.connect(self.set_autostart)
        self.pause.clicked.connect(self.toggle)
        self.folder.clicked.connect(lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTPUT))))
        self.language.currentIndexChanged.connect(self.translate)
        self.theme.currentIndexChanged.connect(self.apply_theme)
        QApplication.styleHints().colorSchemeChanged.connect(self.apply_theme)
        self.tray=QSystemTrayIcon(self.windowIcon(),self)
        self.menu=QMenu();self.show_action=QAction(self);self.quit_action=QAction(self)
        self.autostart_action=QAction(self);self.autostart_action.setCheckable(True)
        self.restart_only_action=QAction(self);self.restart_only_action.setCheckable(True)
        self.restart_only_action.triggered.connect(self.set_restart_only)
        self.show_action.triggered.connect(self.show);self.quit_action.triggered.connect(self.quit)
        self.autostart_action.triggered.connect(self.set_autostart)
        self.menu.aboutToShow.connect(self.refresh_autostart)
        self.menu.addAction(self.show_action);self.menu.addAction(self.restart_only_action);self.menu.addAction(self.autostart_action);self.menu.addAction(self.quit_action)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(lambda reason:self.show() if reason==QSystemTrayIcon.DoubleClick else None)
        self.tray.show()
        self.refresh_restart_only();self.translate();self.apply_theme();self.refresh_autostart();self.resize(560,210)
        self.timer=QTimer(self);self.timer.setInterval(250);self.timer.timeout.connect(self.tick);self.timer.start()

    def text(self,key):return TEXT[self.lang].get(key,key)
    def translate(self,*_):
        self.lang='zh' if self.language.currentIndex()==0 else 'en'
        self.settings.setValue('language',self.lang)
        self.setWindowTitle(self.text('title'));self.note.setText(self.text('hint_restart' if self.monitor.restart_only else 'hint'))
        self.restart_only.setText(self.text('restart_only'));self.restart_only_action.setText(self.text('restart_only'))
        self.folder.setText(self.text('folder'));self.show_action.setText(self.text('show'));self.quit_action.setText(self.text('quit'))
        self.autostart.setText(self.text('autostart'));self.autostart_action.setText(self.text('autostart'))
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
        restart_only=self.monitor.restart_only
        key=state['state']
        if restart_only and key=='cooldown':key='restart_cooldown'
        self.status.setText(self.text(key) + (f" · {state.get('seconds',0):.0f}s" if state.get('seconds') else '')
                            + (f" · {self.text('query_trace')}" if self.monitor.origin.query_trace else ''))
        result=self.monitor.capture_result
        if state['state']=='cooldown':
            self.details.setText(f"{state.get('cooldown_remaining',0):.0f}s")
        elif state['state'] in ('candidate','suspect','storage_limit'):
            self.details.setText(str(OUTPUT))
        else:
            if restart_only and result in ('recovered','recovery_failed'):
                result={'recovered':'restarted','recovery_failed':'restart_failed'}[result]
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
    def refresh_restart_only(self):
        for control in (self.restart_only,self.restart_only_action):
            control.blockSignals(True);control.setChecked(self.monitor.restart_only);control.blockSignals(False)
    def set_restart_only(self,checked):
        self.monitor.set_restart_only(checked)
        self.settings.setValue('restart_only','true' if checked else 'false')
        self.refresh_restart_only();self.translate()
    def refresh_autostart(self,current=None):
        current=Autostart.state() if current is None else current
        tip='' if current is not None else self.text('autostart_missing')
        for control in (self.autostart,self.autostart_action):
            control.blockSignals(True);control.setChecked(bool(current));control.setEnabled(current is not None);control.setToolTip(tip);control.blockSignals(False)
    def set_autostart(self,checked):
        self.refresh_autostart(Autostart.set_enabled(checked))
    def showEvent(self,event):self.refresh_autostart();super().showEvent(event)
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
